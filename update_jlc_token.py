# -*- coding: UTF-8 -*-
"""本地双击提取嘉立创 X-JLC-AccessToken 并自动更新到 GitHub Secrets (TOKEN_LIST)。"""

import argparse
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.parse

from nacl import encoding, public
import requests
from requests.exceptions import RequestException

REPO_OWNER = 'tthking'
REPO_NAME = 'JLC-AutoSign'
SECRET_NAME = 'TOKEN_LIST'
WORKFLOW_FILE = 'jlc-signin.yml'

JLC_HOME_URL = 'https://m.jlc.com/'
JLC_ASSETS_URL = 'https://m.jlc.com/api/appPlatform/center/assets/selectPersonalAssetsInfo'
MOBILE_USER_AGENT = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 '
    'Html5Plus/1.0 (Immersed/20) JlcMobileApp'
)

EDGE_EXECUTABLE = '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'
DEFAULT_PROFILE_DIR = Path(__file__).resolve().parent / '.edge-profile'
REQUEST_TIMEOUT = 15


def mask_value(val):
    if not val or len(val) < 6:
        return '****'
    return f'{val[:2]}****{val[-2:]}'


def unwrap_storage_token(raw_val):
    """解析 localStorage 中读取到的 X-JLC-AccessToken（兼容 uni-app 包装格式）"""
    if raw_val is None:
        return None
    text = str(raw_val).strip()
    if not text or text.upper() == 'NONE':
        return None

    if text.startswith('{'):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and 'data' in parsed:
                inner = str(parsed.get('data') or '').strip()
                return None if (not inner or inner.upper() == 'NONE') else inner
        except ValueError:
            pass

    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return None if (not text or text.upper() == 'NONE') else text


def extract_github_token(credentials_path=None):
    """从环境变量 GITHUB_TOKEN 或 ~/.git-credentials 提取 GitHub PAT"""
    env_token = os.getenv('GITHUB_TOKEN', '').strip()
    if env_token:
        return env_token

    cred_file = Path(credentials_path) if credentials_path else (Path.home() / '.git-credentials')
    if not cred_file.exists():
        raise RuntimeError(f'未找到 {cred_file}，且未设置环境变量 GITHUB_TOKEN')

    for line in cred_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = urllib.parse.urlparse(line)
        if parsed.hostname and parsed.hostname.endswith('github.com') and parsed.password:
            return parsed.password

    raise RuntimeError(f'{cred_file} 中未找到有效的 github.com 凭证')


def validate_jlc_token(access_token):
    """请求嘉立创资产接口验证 Token 是否有效，返回 (is_valid, customer_code, beans)"""
    headers = {
        'X-JLC-AccessToken': access_token,
        'X-JLC-ClientType': 'APP',
        'User-Agent': MOBILE_USER_AGENT,
    }
    try:
        resp = requests.get(JLC_ASSETS_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    except RequestException:
        return False, None, None

    if resp.status_code != 200:
        return False, None, None

    try:
        payload = resp.json()
    except ValueError:
        return False, None, None

    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict) or payload.get('success') is False or payload.get('code') not in (None, 200):
        return False, None, None

    customer_code = str(data.get('customerCode') or '')
    beans = data.get('integralVoucher')
    if not customer_code:
        return False, None, None
    return True, customer_code, beans


def encrypt_secret(public_key_b64, secret_value):
    """使用 GitHub Actions 公钥对 Secret 做 NaCl SealedBox 加密"""
    pub_key = public.PublicKey(public_key_b64.encode('utf-8'), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pub_key)
    encrypted = sealed_box.encrypt(secret_value.encode('utf-8'))
    return base64.b64encode(encrypted).decode('utf-8')


def update_github_secret(github_token, secret_value, owner=REPO_OWNER, repo=REPO_NAME, secret_name=SECRET_NAME):
    """加密并更新指定 GitHub 仓库的 Actions Secret"""
    headers = {
        'Authorization': f'token {github_token}',
        'Accept': 'application/vnd.github+json',
    }
    base_url = f'https://api.github.com/repos/{owner}/{repo}/actions/secrets'

    pk_resp = requests.get(f'{base_url}/public-key', headers=headers, timeout=REQUEST_TIMEOUT)
    pk_resp.raise_for_status()
    pk_data = pk_resp.json()

    encrypted_value = encrypt_secret(pk_data['key'], secret_value)
    put_resp = requests.put(
        f'{base_url}/{secret_name}',
        headers=headers,
        json={'encrypted_value': encrypted_value, 'key_id': pk_data['key_id']},
        timeout=REQUEST_TIMEOUT,
    )
    put_resp.raise_for_status()


def trigger_workflow_check(github_token, owner=REPO_OWNER, repo=REPO_NAME, workflow_file=WORKFLOW_FILE):
    """触发一次 GitHub Actions 验证运行并强制推送 Bark 状态"""
    headers = {
        'Authorization': f'token {github_token}',
        'Accept': 'application/vnd.github+json',
    }
    url = f'https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches'
    resp = requests.post(
        url,
        headers=headers,
        json={'ref': 'master', 'inputs': {'notify_always': 'true'}},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _cdp_eval(ws_url, expression):
    """通过 Chrome DevTools Protocol 在指定页面执行简单 JS 表达式并返回值"""
    import websocket  # 延迟导入以便单元测试无需连接浏览器

    ws = websocket.create_connection(ws_url, timeout=5)
    try:
        ws.send(json.dumps({
            'id': 1,
            'method': 'Runtime.evaluate',
            'params': {'expression': expression, 'returnByValue': True},
        }))
        while True:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get('id') == 1:
                result = msg.get('result', {}).get('result', {})
                return result.get('value')
    finally:
        ws.close()


def _find_jlc_page_ws(port):
    try:
        resp = requests.get(f'http://127.0.0.1:{port}/json/list', timeout=2)
        if resp.status_code != 200:
            return None
        pages = resp.json()
    except (RequestException, ValueError):
        return None

    for item in pages:
        if item.get('type') == 'page' and str(item.get('url', '')).startswith('https://m.jlc.com'):
            return item.get('webSocketDebuggerUrl')
    return None


def capture_token_via_edge(timeout_seconds=300, profile_dir=DEFAULT_PROFILE_DIR):
    """启动独立配置的 Microsoft Edge 手机视图窗口，等待登录后自动提取并验证 Token"""
    if not Path(EDGE_EXECUTABLE).exists():
        raise RuntimeError(f'未找到 Microsoft Edge: {EDGE_EXECUTABLE}')

    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = _find_free_port()

    cmd = [
        EDGE_EXECUTABLE,
        f'--user-data-dir={profile_dir}',
        f'--remote-debugging-port={port}',
        f'--remote-allow-origins=http://127.0.0.1:{port}',
        '--remote-debugging-address=127.0.0.1',
        '--no-first-run',
        '--no-default-browser-check',
        '--window-size=440,860',
        f'--user-agent={MOBILE_USER_AGENT}',
        JLC_HOME_URL,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('🌐 已打开嘉立创专用登录窗口，请在该窗口内登录（若已有有效登录态将自动提取）...')

    invalid_tokens_seen = set()
    cleared_stale_once = False
    deadline = time.monotonic() + timeout_seconds

    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('浏览器窗口已提前关闭，未提取到有效 Token')

            ws_url = _find_jlc_page_ws(port)
            if ws_url:
                try:
                    raw_token = _cdp_eval(ws_url, "window.localStorage.getItem('X-JLC-AccessToken')")
                    token = unwrap_storage_token(raw_token)
                    if token and token not in invalid_tokens_seen:
                        print(f'🔎 检测到 Token（{mask_value(token)}），正在验证有效性...')
                        ok, customer_code, beans = validate_jlc_token(token)
                        if ok:
                            print(f'✅ Token 验证有效！账号: {mask_value(customer_code)}，当前金豆: {beans}')
                            return token, customer_code, beans

                        invalid_tokens_seen.add(token)
                        print('⚠️ 当前缓存的 Token 已失效，请在浏览器窗口中重新登录...')
                        if not cleared_stale_once:
                            cleared_stale_once = True
                            _cdp_eval(ws_url, "window.localStorage.removeItem('X-JLC-AccessToken'); location.reload();")
                except Exception:
                    pass

            time.sleep(1.0)
        raise TimeoutError(f'等待登录超时（超过 {timeout_seconds} 秒）')
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def notify_macos(title, message):
    """发送 macOS 系统通知（失败时静默忽略）"""
    try:
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
        subprocess.run(['osascript', '-e', script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description='自动提取嘉立创 Token 并更新至 GitHub Secrets')
    parser.add_argument('--timeout', type=int, default=300, help='等待登录超时秒数（默认 300）')
    parser.add_argument('--no-trigger', action='store_true', help='更新后不自动触发 GitHub Actions 验证运行')
    args = parser.parse_args()

    try:
        github_token = extract_github_token()
        jlc_token, customer_code, beans = capture_token_via_edge(timeout_seconds=args.timeout)
        print(f'🔐 正在加密并写入 GitHub Secrets ({REPO_OWNER}/{REPO_NAME}:{SECRET_NAME})...')
        update_github_secret(github_token, jlc_token)
        print('✅ GitHub Secrets TOKEN_LIST 更新成功！')

        if not args.no_trigger:
            trigger_workflow_check(github_token)
            print('🚀 已触发一次 GitHub Actions 云端验证（稍后手机 Bark 会收到状态通知）')

        notify_macos('JLC Token 更新成功', f'账号 {mask_value(customer_code)}（金豆 {beans}）已同步至 GitHub Secrets')
        return 0
    except Exception as e:
        print(f'❌ 更新失败: {e}')
        notify_macos('JLC Token 更新失败', str(e))
        return 1


if __name__ == '__main__':
    sys.exit(main())
