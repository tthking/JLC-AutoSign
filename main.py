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
PREVIEW_COUPONS = os.getenv('PREVIEW_COUPONS', 'false').strip().lower() in ('1', 'true', 'yes')
SEVENTH_DAY_PRIORITY = os.getenv('SEVENTH_DAY_PRIORITY', '纸盒,FPC,金豆')

# 接口配置
SIGN_URL = 'https://m.jlc.com/api/activity/sign/signIn?source=3'
GOLD_BEAN_URL = 'https://m.jlc.com/api/appPlatform/center/assets/selectPersonalAssetsInfo'
SIGN_CONFIG_URL = 'https://m.jlc.com/api/activity/sign/getCurrentUserSignInConfig'
COUPON_LIST_URL = 'https://m.jlc.com/api/activity/sign/selectCouponList'
RECEIVE_COUPON_URL = 'https://m.jlc.com/api/activity/sign/receiveCoupon'
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


def parse_priority_list(raw):
    """解析第7天领奖优先级配置，并确保始终保留金豆作为最终兜底"""
    items = [part.strip() for part in str(raw or '').split(',') if part.strip()]
    if not items:
        return ['纸盒', 'FPC', '金豆']

    seen = set()
    ordered = []
    for item in items:
        key = '金豆' if item in ('金豆', 'BEAN', 'bean') else item
        if key.upper() not in seen:
            seen.add(key.upper())
            ordered.append(key)

    if '金豆' not in ordered:
        ordered.append('金豆')
    return ordered


# ======== 推送通知 ========
def send_msg_by_bark(send_key, title, content, retries=2, backoff=3):
    """发送 Bark 推送通知（含瞬态网络/5xx 重试），支持自定义完整 URL 或默认 api.day.app Key"""
    key = send_key.strip().rstrip('/')
    if key.startswith('http://') or key.startswith('https://'):
        push_url = key
    else:
        push_url = f'https://api.day.app/{key}'

    data = {
        'title': title,
        'body': content,
    }
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(push_url, json=data, timeout=REQUEST_TIMEOUT)
            if 400 <= response.status_code < 500:
                print(f"❌ Bark 推送被拒绝 (HTTP {response.status_code})，请检查 SEND_KEY_LIST")
                return None
            response.raise_for_status()
            return response.json()
        except (RequestException, ValueError) as e:
            print(f"⚠️ Bark 推送第 {attempt}/{retries} 次失败: {e}")
            if attempt < retries and backoff > 0:
                time.sleep(backoff * attempt)
    print('❌ Bark 推送多次重试后仍失败')
    return None


# ======== HTTP 请求辅助 ========
def request_json(session, target_url, headers, params=None):
    """发送 GET 请求并解析 JSON，统一识别 HTTP 401/403 与非 JSON 响应"""
    response = session.get(target_url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
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


# ======== 第7天优惠券与金豆逻辑 ========
def _extract_coupon_info(item):
    """标准化 selectCouponList 返回的单个优惠券对象"""
    if not isinstance(item, dict):
        return None
    dto = item.get('hubCustomerCouponRespDTO')
    if not isinstance(dto, dict):
        dto = {}

    remainder_raw = item.get('remainderInventory')
    try:
        remainder = int(remainder_raw) if remainder_raw is not None else None
    except (TypeError, ValueError):
        remainder = None

    return {
        'id': item.get('id'),
        'title': str(item.get('goodsTitle') or '').strip(),
        'name': str(dto.get('name') or '').strip(),
        'business_line': str(dto.get('businessLine') or '').strip().upper(),
        'can_exchange': item.get('isCanExchange'),
        'remainder': remainder,
        'cycle_limit_unit': item.get('cycleLimitUnit'),
        'cycle_limit': item.get('cycleLimit'),
    }


def _is_coupon_claimable(info):
    """判断优惠券当前是否可领（isCanExchange 为 True 且有库存）"""
    if not info or info.get('id') is None:
        return False
    if info.get('can_exchange') is not True:
        return False
    remainder = info.get('remainder')
    if remainder is not None and remainder <= 0:
        return False
    return True


def _matches_coupon_keyword(info, keyword):
    """按业务线编码或名称关键字匹配目标优惠券"""
    kw = keyword.strip().upper()
    title_upper = info['title'].upper()
    name_upper = info['name'].upper()
    biz = info['business_line']

    if kw in ('纸盒', 'BOX'):
        return biz == 'BOX' or '纸盒' in title_upper or '纸盒' in name_upper
    if kw == 'FPC':
        return biz == 'FPC' or 'FPC' in title_upper or 'FPC' in name_upper
    return biz == kw or kw in title_upper or kw in name_upper


def _format_coupon_summary(coupons):
    """生成用于日志和 Bark 告警的可选券简要清单"""
    parts = []
    for c in coupons:
        label = c['title'] or c['name'] or '未命名券'
        biz = c['business_line'] or '-'
        state = '可领' if _is_coupon_claimable(c) else ('已兑完' if c['remainder'] == 0 else '已上限/不可领')
        parts.append(f"{label}[id={c['id']},biz={biz},{state}]")
    return '；'.join(parts) if parts else '空列表'


def _fetch_coupon_list(session, headers):
    """查询第7天可选优惠券列表，返回 (coupons, error_message)"""
    try:
        res = request_json(session, COUPON_LIST_URL, headers)
    except TokenExpiredError:
        raise
    except (RequestException, ApiResponseError) as e:
        return None, f'查询券列表请求失败: {e}'

    if not _is_api_success(res):
        return None, f"查询券列表接口返回失败: {res.get('message') or res.get('code')}"

    raw_list = res.get('data')
    if not isinstance(raw_list, list):
        return None, f'查询券列表返回非列表结构: {type(raw_list).__name__}'

    coupons = [info for info in (_extract_coupon_info(item) for item in raw_list) if info is not None]
    if not coupons:
        return [], '券列表为空'
    return coupons, None


def _claim_coupon(session, headers, coupon_info):
    """调用 receiveCoupon 领取指定优惠券，返回 (ok, error_message)"""
    coupon_id = coupon_info['id']
    for terminal in ('APP', 'WEB'):
        try:
            res = request_json(
                session,
                RECEIVE_COUPON_URL,
                headers,
                params={'couponId': coupon_id, 'terminal': terminal},
            )
        except TokenExpiredError:
            raise
        except (RequestException, ApiResponseError) as e:
            return False, str(e)

        if _is_api_success(res):
            return True, ''

        msg = str(res.get('message') or f"code={res.get('code')}")
        if _is_auth_error_message(msg, res.get('code')):
            raise TokenExpiredError(msg)
        # 若因 terminal 参数不兼容导致失败，自动尝试另一端参数
        if terminal == 'APP' and any(k in msg.lower() for k in ('terminal', '终端', '参数')):
            continue
        return False, msg

    return False, '领券接口拒绝了请求参数'


def _claim_voucher_beans(session, headers, account_label, integral_voucher, reason_note='', warnings=None):
    """调用 receiveVoucher 领取第7天金豆，并在触发告警时保留诊断信息"""
    warnings = list(warnings or [])
    last_err = ''
    for params in (None, {'platformType': 'WEB'}):
        try:
            res = request_json(session, SEVENTH_DAY_URL, headers, params=params)
        except TokenExpiredError:
            raise
        except (RequestException, ApiResponseError) as e:
            last_err = str(e)
            break

        if _is_api_success(res):
            seventh_data = res.get('data') or {}
            bonus = int(seventh_data.get('gainNum') or 8)
            note_part = f'（{reason_note}）' if reason_note else ''
            print(f"🎉 [账号{account_label}] 第七天签到成功{note_part}，获取 {bonus} 个金豆")
            msg = f'🎉 账号({account_label})：第七天签到成功{note_part}，获取{bonus}个金豆，当前总数：{integral_voucher + bonus}'
            if warnings:
                msg += '\n⚠️ 第7天诊断信息：' + '；'.join(warnings)
            return 'gained', msg, warnings

        last_err = str(res.get('message') or f"code={res.get('code')}")
        if _is_auth_error_message(last_err, res.get('code')):
            raise TokenExpiredError(last_err)
        if params is None and any(k in last_err.lower() for k in ('platform', '平台', '参数')):
            continue
        break

    err_msg = last_err or '第七天奖励领取失败'
    print(f"❌ [账号{account_label}] 第七天奖励领取失败 - {err_msg}")
    warnings.append(f'领取8金豆失败: {err_msg}')
    return 'failed', f"❌ 账号({account_label})：第七天奖励领取失败 - {'；'.join(warnings)}", warnings


def _claim_seventh_day(session, headers, account_label, integral_voucher):
    """按优先级顺序领取第7天奖励（默认：纸盒券 -> FPC券 -> 8金豆兜底），并记录异常告警"""
    priorities = parse_priority_list(SEVENTH_DAY_PRIORITY)
    coupon_targets = [p for p in priorities if p != '金豆']
    warnings = []

    if not coupon_targets:
        return _claim_voucher_beans(session, headers, account_label, integral_voucher)

    coupons, fetch_err = _fetch_coupon_list(session, headers)
    if fetch_err:
        print(f"⚠️ [账号{account_label}] {fetch_err}，将自动兜底领取金豆")
        warnings.append(f'{fetch_err}，已自动兜底领取金豆')
        return _claim_voucher_beans(
            session, headers, account_label, integral_voucher,
            reason_note='券列表查询异常，兜底金豆', warnings=warnings
        )

    print(f"🎫 [账号{account_label}] 第7天可选优惠券: {_format_coupon_summary(coupons)}")

    matched_any_target = False
    exhausted_targets = []

    for keyword in coupon_targets:
        matched = [c for c in coupons if _matches_coupon_keyword(c, keyword)]
        if not matched:
            print(f"⚠️ [账号{account_label}] 可选券列表中未匹配到目标【{keyword}】")
            continue

        matched_any_target = True
        claimable = [c for c in matched if _is_coupon_claimable(c)]
        if not claimable:
            print(f"ℹ️ [账号{account_label}] 目标【{keyword}】本月已达上限或已兑完，跳过")
            exhausted_targets.append(keyword)
            continue

        if len(claimable) > 1:
            warnings.append(f'目标【{keyword}】匹配到多张可领券({_format_coupon_summary(claimable)})，已优先选取第一张')

        chosen = claimable[0]
        coupon_label = chosen['title'] or chosen['name'] or keyword
        ok, claim_err = _claim_coupon(session, headers, chosen)
        if ok:
            print(f"🎉 [账号{account_label}] 第七天签到成功，已领取优惠券：{coupon_label} (id={chosen['id']})")
            msg = f"🎉 账号({account_label})：第七天签到成功，已领取【{coupon_label}】（当前金豆：{integral_voucher}）"
            if warnings:
                msg += '\n⚠️ 第7天诊断信息：' + '；'.join(warnings)
            return 'gained', msg, warnings

        print(f"⚠️ [账号{account_label}] 领取【{coupon_label}】失败: {claim_err}")
        warnings.append(f"领取【{coupon_label}】(id={chosen['id']})失败: {claim_err}")

    if not matched_any_target:
        summary = _format_coupon_summary(coupons)
        warnings.append(f"未在列表中匹配到任何目标券({','.join(coupon_targets)})，可能官方改名；当前列表：{summary}")
        reason = '未匹配到目标券，兜底金豆'
    elif exhausted_targets and not warnings:
        reason = f"{'/'.join(exhausted_targets)}券本月已达上限或已兑完"
    else:
        reason = '目标券领取失败，兜底金豆'

    return _claim_voucher_beans(
        session, headers, account_label, integral_voucher,
        reason_note=reason, warnings=warnings
    )


def _attach_coupon_preview(session, headers, account_label, summary_msg, warnings):
    """调试模式：只读查询当前第7天可选券列表并附加到通知与日志"""
    coupons, fetch_err = _fetch_coupon_list(session, headers)
    if fetch_err:
        warn = f'预览第7天券列表失败: {fetch_err}'
        print(f"⚠️ [账号{account_label}] {warn}")
        warnings.append(warn)
        return f'{summary_msg}\n⚠️ {warn}'

    summary = _format_coupon_summary(coupons)
    print(f"🔍 [账号{account_label}] 第7天券列表预览: {summary}")
    return f'{summary_msg}\n🎫 第7天券列表预览：{summary}'


def _sign_in_once(session, access_token):
    """执行单次账号校验与签到流程，返回 (status_kind, summary_message, warnings)"""
    headers = {
        'X-JLC-AccessToken': access_token,
        'X-JLC-ClientType': 'APP',
        'User-Agent': (
            'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) '
            'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Html5Plus/1.0 (Immersed/20) JlcMobileApp'
        ),
    }
    warnings = []

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

    def finalize(kind, msg, extra_warnings=None):
        all_warnings = list(warnings) + list(extra_warnings or [])
        if PREVIEW_COUPONS:
            msg = _attach_coupon_preview(session, headers, account_label, msg, all_warnings)
        return kind, msg, all_warnings

    # 2. 预检当日签到状态（如遇非鉴权异常则降级直接请求签到接口）
    try:
        config_result = request_json(session, SIGN_CONFIG_URL, headers)
        config_data = config_result.get('data')
        if _is_api_success(config_result) and isinstance(config_data, dict):
            if config_data.get('haveSignIn'):
                if config_data.get('haveReceive') is False:
                    kind, msg, w = _claim_seventh_day(session, headers, account_label, integral_voucher)
                    return finalize(kind, msg, w)
                print(f"ℹ️ [账号{account_label}] 今日已签到，当前金豆：{integral_voucher}")
                return finalize('already_signed', f'ℹ️ 账号({account_label})：今日已签到，当前金豆：{integral_voucher}')
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
            return finalize('already_signed', f'ℹ️ 账号({account_label})：今日已签到，当前金豆：{integral_voucher}')
        if _is_auth_error_message(message, sign_result.get('code')):
            raise TokenExpiredError(message)
        print(f"❌ [账号{account_label}] 签到失败 - {message}")
        return finalize('failed', f'❌ 账号({account_label})：签到失败 - {message}')

    data = sign_result.get('data') or {}
    gain_num = data.get('gainNum')
    status = data.get('status')

    # status: 0=失败, 1=成功, 2=今日已签到
    if status == 2:
        print(f"ℹ️ [账号{account_label}] 今日已签到，当前金豆：{integral_voucher}")
        return finalize('already_signed', f'ℹ️ 账号({account_label})：今日已签到，当前金豆：{integral_voucher}')

    if status == 1:
        if gain_num is not None and int(gain_num) > 0:
            beans = int(gain_num)
            print(f"✅ [账号{account_label}] 今日签到成功，获取 {beans} 个金豆")
            return finalize('gained', f'✅ 账号({account_label})：获取{beans}个金豆，当前总数：{integral_voucher + beans}')
        kind, msg, w = _claim_seventh_day(session, headers, account_label, integral_voucher)
        return finalize(kind, msg, w)

    msg = sign_result.get('message') or f'签到返回异常状态 status={status}'
    print(f"❌ [账号{account_label}] {msg}")
    return finalize('failed', f'❌ 账号({account_label})：{msg}')


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
                return 'failed', f'❌ 账号({token_label})：Token 已失效，请重新抓取 X-JLC-AccessToken 更新 Secrets（{e}）', []
            except (RequestException, ApiResponseError) as e:
                print(f"⚠️ [账号{token_label}] 第 {attempt}/{retries} 次请求失败: {e}")
                if attempt < retries:
                    wait = backoff * attempt
                    if wait > 0:
                        print(f"⏳ {wait} 秒后重试...")
                        time.sleep(wait)
                else:
                    print(f"❌ [账号{token_label}] 已重试 {retries} 次，放弃")
                    return 'failed', f'❌ 账号({token_label})：连续 {retries} 次请求失败（{e}）', []
            except Exception as e:
                print(f"❌ [账号{token_label}] 未知错误: {e}")
                return 'failed', f'❌ 账号({token_label})：未知错误（{e}）', []
    return 'failed', f'❌ 账号({token_label})：签到未执行', []


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
        outcomes = {'gained': [], 'already_signed': [], 'failed': [], 'warnings': []}

        for i, token in enumerate(group_tokens):
            print(f"📝 处理第 {i + 1}/{len(group_tokens)} 个账号...")
            res = sign_in(token)
            if len(res) == 2:
                status_kind, message = res
                acct_warnings = []
            else:
                status_kind, message, acct_warnings = res

            outcomes.setdefault(status_kind, []).append(message)
            outcomes['warnings'].extend(acct_warnings)
            if status_kind == 'failed':
                total_failed += 1

            if i < len(group_tokens) - 1:
                wait_time = random.randint(5, 15)
                print(f"⏳ 等待 {wait_time} 秒后处理下一个账号...")
                time.sleep(wait_time)

        group_outcomes[send_key] = outcomes

    print('\n📬 开始检查是否需要发送通知...')
    notification_sent = False
    force_notify = NOTIFY_ALWAYS or PREVIEW_COUPONS

    for send_key, outcomes in group_outcomes.items():
        gained = outcomes['gained']
        already = outcomes['already_signed']
        failed = outcomes['failed']
        warnings = outcomes['warnings']

        if failed:
            title = '⚠️ 嘉立创签到汇总（含异常）' if gained else '⚠️ 嘉立创自动签到异常'
            messages = gained + failed + (already if force_notify else [])
        elif warnings:
            title = '⚠️ 嘉立创签到汇总（含第7天告警）'
            messages = gained + (already if (force_notify or not gained) else [])
        elif gained:
            title = '嘉立创签到汇总'
            messages = gained + (already if force_notify else [])
        elif force_notify and already:
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
