# -*- coding: UTF-8 -*-
from collections import defaultdict
import os
import random
import sys
import time

import requests
from requests.exceptions import RequestException

TOKEN_LIST = os.getenv('TOKEN_LIST', '')
SEND_KEY_LIST = os.getenv('SEND_KEY_LIST', '')
NOTIFY_ALWAYS = os.getenv('NOTIFY_ALWAYS', 'false').strip().lower() in ('1', 'true', 'yes')

# 接口配置
SIGN_URL = 'https://m.jlc.com/api/activity/sign/signIn?source=3'
GOLD_BEAN_URL = 'https://m.jlc.com/api/appPlatform/center/assets/selectPersonalAssetsInfo'
SIGN_CONFIG_URL = 'https://m.jlc.com/api/activity/sign/getCurrentUserSignInConfig'
SEVENTH_DAY_URL = 'https://m.jlc.com/api/activity/sign/receiveVoucher'

REQUEST_TIMEOUT = 15
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 10


class TokenExpiredError(Exception):
    """AccessToken 失效或未授权异常（无需重试）"""


class ApiResponseError(Exception):
    """接口返回非预期内容或业务异常"""


# ======== 工具函数 ========
def mask_account(account):
    """用于打印时隐藏部分账号信息"""
    if len(account) >= 4:
        return account[:2] + '****' + account[-2:]
    return '****'


def mask_json_customer_code(data):
    """递归地脱敏 JSON 中的 customerCode 字段"""
    if isinstance(data, dict):
        new_data = {}
        for k, v in data.items():
            if k == 'customerCode' and isinstance(v, str):
                new_data[k] = v[:1] + 'xxxxx' + v[-2:]  # 例: 1xxxxx8A
            else:
                new_data[k] = mask_json_customer_code(v)
        return new_data
    if isinstance(data, list):
        return [mask_json_customer_code(i) for i in data]
    return data


def _is_auth_error_message(message, code=None):
    """判断业务报错是否属于 Token 失效/未登录"""
    if code in (401, 403):
        return True
    msg_lower = str(message or '').lower()
    return any(keyword in msg_lower for keyword in ('未登录', '登录', 'token', '过期', '失效', 'unauthorized'))


def _is_api_success(payload):
    """判断接口业务响应是否成功（兼容仅返回 code 或同时返回 success 的响应）"""
    if payload.get('success') is False:
        return False
    code = payload.get('code')
    return code is None or code == 200


def parse_config_pairs(token_raw, send_key_raw):
    """解析并配对 TOKEN_LIST 与 SEND_KEY_LIST，支持单个 SendKey 广播到多个账号"""
    tokens = [t.strip() for t in token_raw.split(',') if t.strip()]
    send_keys = [k.strip() for k in send_key_raw.split(',') if k.strip()]

    if not tokens or not send_keys:
        return tokens, send_keys, []

    if len(send_keys) == 1 and len(tokens) > 1:
        return tokens, send_keys, [(token, send_keys[0]) for token in tokens]

    if len(send_keys) != len(tokens):
        print(
            f"⚠️ TOKEN_LIST({len(tokens)}) 与 SEND_KEY_LIST({len(send_keys)}) 数量不一致，"
            f"将仅处理前 {min(len(tokens), len(send_keys))} 个账号"
        )

    pairs = list(zip(tokens, send_keys))
    return tokens, send_keys, pairs


# ======== 推送通知 ========
def send_msg_by_bark(send_key, title, content):
    """发送 Bark 推送通知，支持自定义完整 URL 或默认 api.day.app Key"""
    key = send_key.strip().rstrip('/')
    if key.startswith('http://') or key.startswith('https://'):
        push_url = key
    else:
        push_url = f'https://api.day.app/{key}'

    data = {
        'title': title,
        'body': content,
    }
    try:
        response = requests.post(push_url, json=data, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (RequestException, ValueError) as e:
        print(f"❌ Bark 推送异常: {e}")
        return None


# ======== HTTP 请求辅助 ========
def request_json(session, target_url, headers):
    """发送 GET 请求并解析 JSON，统一识别 HTTP 401/403 与非 JSON 响应"""
    response = session.get(target_url, headers=headers, timeout=REQUEST_TIMEOUT)
    if response.status_code in (401, 403):
        raise TokenExpiredError(f'HTTP {response.status_code}，AccessToken 已失效或未登录')
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as e:
        snippet = response.text[:120].replace('\n', ' ')
        raise ApiResponseError(f'接口返回非 JSON 数据: {snippet}') from e

    if not isinstance(payload, dict):
        raise ApiResponseError('接口返回的 JSON 根结构不是对象')
    return payload


def _claim_seventh_day(session, headers, account_label, integral_voucher):
    """领取第七天签到额外奖励"""
    seventh_result = request_json(session, SEVENTH_DAY_URL, headers)
    if _is_api_success(seventh_result):
        seventh_data = seventh_result.get('data') or {}
        bonus = int(seventh_data.get('gainNum') or 8)
        print(f"🎉 [账号{account_label}] 第七天签到成功，获取 {bonus} 个金豆")
        return 'gained', f'🎉 账号({account_label})：第七天签到成功，获取{bonus}个金豆，当前总数：{integral_voucher + bonus}'

    msg = seventh_result.get('message') or '第七天奖励领取失败'
    print(f"❌ [账号{account_label}] 第七天奖励领取失败 - {msg}")
    return 'failed', f'❌ 账号({account_label})：第七天奖励领取失败 - {msg}'


def _sign_in_once(session, access_token):
    """执行单次账号校验与签到流程，返回 (status_kind, summary_message)"""
    headers = {
        'X-JLC-AccessToken': access_token,
        'X-JLC-ClientType': 'APP',
        'User-Agent': (
            'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) '
            'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Html5Plus/1.0 (Immersed/20) JlcMobileApp'
        ),
    }

    # 1. 查询资产与 Token 有效性（同时获取 customerCode 和当前金豆数）
    bean_result = request_json(session, GOLD_BEAN_URL, headers)
    bean_data = bean_result.get('data')
    if not _is_api_success(bean_result) or not isinstance(bean_data, dict):
        msg = bean_result.get('message') or '资产接口返回异常'
        if _is_auth_error_message(msg, bean_result.get('code')):
            raise TokenExpiredError(msg)
        raise ApiResponseError(msg)

    customer_code = str(bean_data.get('customerCode') or '')
    integral_voucher = int(bean_data.get('integralVoucher') or 0)
    account_label = mask_account(customer_code or access_token)

    # 2. 预检当日签到状态（如遇非鉴权异常则降级直接请求签到接口）
    try:
        config_result = request_json(session, SIGN_CONFIG_URL, headers)
        config_data = config_result.get('data')
        if _is_api_success(config_result) and isinstance(config_data, dict):
            if config_data.get('haveSignIn'):
                if config_data.get('haveReceive') is False:
                    return _claim_seventh_day(session, headers, account_label, integral_voucher)
                print(f"ℹ️ [账号{account_label}] 今日已签到，当前金豆：{integral_voucher}")
                return 'already_signed', f'ℹ️ 账号({account_label})：今日已签到，当前金豆：{integral_voucher}'
    except TokenExpiredError:
        raise
    except (RequestException, ApiResponseError) as e:
        print(f"⚠️ [账号{account_label}] 签到状态预检失败，尝试直接签到: {e}")

    # 3. 执行签到请求
    sign_result = request_json(session, SIGN_URL, headers)
    if not _is_api_success(sign_result):
        message = str(sign_result.get('message') or '未知错误')
        if '已经签到' in message or '已签到' in message:
            print(f"ℹ️ [账号{account_label}] 今日已签到，当前金豆：{integral_voucher}")
            return 'already_signed', f'ℹ️ 账号({account_label})：今日已签到，当前金豆：{integral_voucher}'
        if _is_auth_error_message(message, sign_result.get('code')):
            raise TokenExpiredError(message)
        print(f"❌ [账号{account_label}] 签到失败 - {message}")
        return 'failed', f'❌ 账号({account_label})：签到失败 - {message}'

    data = sign_result.get('data') or {}
    gain_num = data.get('gainNum')
    status = data.get('status')

    # status: 0=失败, 1=成功, 2=今日已签到
    if status == 2:
        print(f"ℹ️ [账号{account_label}] 今日已签到，当前金豆：{integral_voucher}")
        return 'already_signed', f'ℹ️ 账号({account_label})：今日已签到，当前金豆：{integral_voucher}'

    if status == 1:
        if gain_num is not None and int(gain_num) > 0:
            beans = int(gain_num)
            print(f"✅ [账号{account_label}] 今日签到成功，获取 {beans} 个金豆")
            return 'gained', f'✅ 账号({account_label})：获取{beans}个金豆，当前总数：{integral_voucher + beans}'
        return _claim_seventh_day(session, headers, account_label, integral_voucher)

    msg = sign_result.get('message') or f'签到返回异常状态 status={status}'
    print(f"❌ [账号{account_label}] {msg}")
    return 'failed', f'❌ 账号({account_label})：{msg}'


# ======== 单个账号签到逻辑 ========
def sign_in(access_token, retries=DEFAULT_RETRIES, backoff=DEFAULT_BACKOFF):
    """执行单个账号签到（含网络异常重试与 Token 失效快速失败）"""
    token_label = mask_account(access_token)
    with requests.Session() as session:
        for attempt in range(1, retries + 1):
            try:
                return _sign_in_once(session, access_token)
            except TokenExpiredError as e:
                print(f"❌ [账号{token_label}] Token 已失效: {e}")
                return 'failed', f'❌ 账号({token_label})：Token 已失效，请重新抓取 X-JLC-AccessToken 更新 Secrets（{e}）'
            except (RequestException, ApiResponseError) as e:
                print(f"⚠️ [账号{token_label}] 第 {attempt}/{retries} 次请求失败: {e}")
                if attempt < retries:
                    wait = backoff * attempt
                    if wait > 0:
                        print(f"⏳ {wait} 秒后重试...")
                        time.sleep(wait)
                else:
                    print(f"❌ [账号{token_label}] 已重试 {retries} 次，放弃")
                    return 'failed', f'❌ 账号({token_label})：连续 {retries} 次请求失败（{e}）'
            except Exception as e:
                print(f"❌ [账号{token_label}] 未知错误: {e}")
                return 'failed', f'❌ 账号({token_label})：未知错误（{e}）'
    return 'failed', f'❌ 账号({token_label})：签到未执行'


# ======== 主函数 ========
def main():
    tokens, send_keys, pairs = parse_config_pairs(TOKEN_LIST, SEND_KEY_LIST)

    if not tokens:
        print('❌ 请设置 TOKEN_LIST')
        for send_key in dict.fromkeys(send_keys):
            send_msg_by_bark(send_key, '⚠️ 嘉立创自动签到异常', '❌ 未配置 TOKEN_LIST，请检查 GitHub Secrets。')
        return 1

    if not send_keys:
        print('❌ 请设置 SEND_KEY_LIST')
        return 1

    print(f"🔧 共发现 {len(pairs)} 个账号需要签到")

    task_groups = defaultdict(list)
    for access_token, send_key in pairs:
        task_groups[send_key].append(access_token)

    print(f"📊 共分为 {len(task_groups)} 个通知组")

    group_outcomes = {}
    total_failed = 0

    for send_key, group_tokens in task_groups.items():
        print(f"\n🚀 开始处理 SendKey: {send_key[:5]}... 的 {len(group_tokens)} 个账号")
        outcomes = {'gained': [], 'already_signed': [], 'failed': []}

        for i, token in enumerate(group_tokens):
            print(f"📝 处理第 {i + 1}/{len(group_tokens)} 个账号...")
            status_kind, message = sign_in(token)
            outcomes.setdefault(status_kind, []).append(message)
            if status_kind == 'failed':
                total_failed += 1

            if i < len(group_tokens) - 1:
                wait_time = random.randint(5, 15)
                print(f"⏳ 等待 {wait_time} 秒后处理下一个账号...")
                time.sleep(wait_time)

        group_outcomes[send_key] = outcomes

    print('\n📬 开始检查是否需要发送通知...')
    notification_sent = False

    for send_key, outcomes in group_outcomes.items():
        gained = outcomes['gained']
        already = outcomes['already_signed']
        failed = outcomes['failed']

        if failed:
            title = '⚠️ 嘉立创签到汇总（含异常）' if gained else '⚠️ 嘉立创自动签到异常'
            messages = gained + failed + (already if NOTIFY_ALWAYS else [])
        elif gained:
            title = '嘉立创签到汇总'
            messages = gained + (already if NOTIFY_ALWAYS else [])
        elif NOTIFY_ALWAYS and already:
            title = '嘉立创签到状态'
            messages = already
        else:
            print(f"⏭️ SendKey: {send_key[:5]}... 组内无新金豆或异常，跳过通知")
            continue

        content = '\n\n'.join(messages)
        print(f"📤 准备发送通知给 SendKey: {send_key[:5]}...（标题：{title}）")
        response = send_msg_by_bark(send_key, title, content)

        if response and response.get('code') == 200:
            print('✅ 通知发送成功！')
            notification_sent = True
        else:
            error_msg = response.get('message') if response else '未知错误'
            print(f"❌ 通知发送失败！错误: {error_msg}")

    if not notification_sent:
        print('ℹ️ 本次运行未发送通知')

    if total_failed > 0:
        print(f"❌ 本次共有 {total_failed} 个账号签到失败")
        return 1
    return 0


# ======== 程序入口 ========
if __name__ == '__main__':
    print('🏁 嘉立创自动签到任务开始')
    exit_code = main()
    print('🏁 任务执行完毕')
    sys.exit(exit_code)
