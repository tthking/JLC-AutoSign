import unittest
from unittest.mock import MagicMock, patch

import requests

import main


def make_response(status_code=200, json_data=None, text=''):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f'{status_code} Error', response=resp
        )
    else:
        resp.raise_for_status.return_value = None

    if json_data is None:
        resp.json.side_effect = ValueError('No JSON')
    else:
        resp.json.return_value = json_data
    return resp


def make_coupon_item(cid, title, biz, can_exchange=True, remainder=10, dto_name=''):
    return {
        'id': cid,
        'goodsTitle': title,
        'isCanExchange': can_exchange,
        'remainderInventory': remainder,
        'cycleLimitUnit': 3,
        'cycleLimit': 1,
        'hubCustomerCouponRespDTO': {
            'businessLine': biz,
            'name': dto_name or title,
        },
    }


class TestHelpers(unittest.TestCase):
    def test_mask_account(self):
        self.assertEqual(main.mask_account('12345678'), '12****78')
        self.assertEqual(main.mask_account('abc'), '****')

    def test_mask_json_customer_code(self):
        data = {'customerCode': '1234568A', 'nested': [{'customerCode': 'ABCDEF'}]}
        masked = main.mask_json_customer_code(data)
        self.assertEqual(masked['customerCode'], '1xxxxx8A')
        self.assertEqual(masked['nested'][0]['customerCode'], 'AxxxxxEF')

    def test_parse_config_pairs_single_send_key_broadcast(self):
        tokens, keys, pairs = main.parse_config_pairs('t1, t2 , ', 'k1')
        self.assertEqual(tokens, ['t1', 't2'])
        self.assertEqual(keys, ['k1'])
        self.assertEqual(pairs, [('t1', 'k1'), ('t2', 'k1')])

    def test_parse_config_pairs_mismatch(self):
        _, _, pairs = main.parse_config_pairs('t1,t2,t3', 'k1,k2')
        self.assertEqual(pairs, [('t1', 'k1'), ('t2', 'k2')])

    def test_parse_priority_list(self):
        self.assertEqual(main.parse_priority_list('纸盒, FPC'), ['纸盒', 'FPC', '金豆'])
        self.assertEqual(main.parse_priority_list(''), ['纸盒', 'FPC', '金豆'])
        self.assertEqual(main.parse_priority_list('FPC,纸盒,FPC,金豆'), ['FPC', '纸盒', '金豆'])


class TestBark(unittest.TestCase):
    @patch('main.requests.post')
    def test_send_msg_by_bark_default_and_custom_url(self, mock_post):
        mock_post.return_value = make_response(200, {'code': 200, 'message': 'success'})
        res1 = main.send_msg_by_bark('abc123', '标题', '内容')
        self.assertEqual(res1['code'], 200)
        self.assertEqual(mock_post.call_args[0][0], 'https://api.day.app/abc123')

        res2 = main.send_msg_by_bark('https://bark.example.com/push/', '标题', '内容')
        self.assertEqual(res2['code'], 200)
        self.assertEqual(mock_post.call_args[0][0], 'https://bark.example.com/push')

    @patch('main.time.sleep')
    @patch('main.requests.post')
    def test_send_msg_by_bark_non_json(self, mock_post, mock_sleep):
        mock_post.return_value = make_response(502, json_data=None, text='Bad Gateway')
        self.assertIsNone(main.send_msg_by_bark('abc123', '标题', '内容'))
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(3)

    @patch('main.time.sleep')
    @patch('main.requests.post')
    def test_send_msg_by_bark_retries_and_succeeds(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            requests.Timeout('bark timeout'),
            make_response(200, {'code': 200, 'message': 'success'}),
        ]
        res = main.send_msg_by_bark('abc123', '标题', '内容')
        self.assertEqual(res['code'], 200)
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(3)

    @patch('main.requests.post')
    def test_send_msg_by_bark_4xx_no_retry(self, mock_post):
        mock_post.return_value = make_response(400, {'code': 400, 'message': 'bad key'})
        self.assertIsNone(main.send_msg_by_bark('bad_key', '标题', '内容'))
        self.assertEqual(mock_post.call_count, 1)


class TestSignIn(unittest.TestCase):
    def _assets_ok(self, voucher=100):
        return make_response(
            200,
            {
                'code': 200,
                'success': True,
                'data': {'customerCode': '1234568A', 'integralVoucher': voucher},
            },
        )

    def _config_not_signed(self):
        return make_response(
            200,
            {'code': 200, 'success': True, 'data': {'haveSignIn': False, 'haveReceive': True}},
        )

    @patch('main.requests.Session')
    def test_sign_in_daily_success(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.side_effect = [
            self._assets_ok(100),
            self._config_not_signed(),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 1, 'gainNum': 4}}),
        ]
        kind, msg, warnings = main.sign_in('valid_token', retries=2, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertEqual(warnings, [])
        self.assertIn('获取4个金豆', msg)
        self.assertIn('当前总数：104', msg)

    @patch('main.requests.Session')
    def test_seventh_day_claims_box_coupon_first(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        coupon_list = [
            make_coupon_item(101, '10元FPC优惠券', 'PCB', can_exchange=True, dto_name='FPC满减券'),
            make_coupon_item(102, '15元纸盒包装券', 'BOX', can_exchange=True),
        ]
        session.get.side_effect = [
            self._assets_ok(200),
            self._config_not_signed(),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 1, 'gainNum': 0}}),
            make_response(200, {'code': 200, 'success': True, 'data': coupon_list}),
            make_response(200, {'code': 200, 'success': True, 'data': True}),
        ]
        kind, msg, warnings = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertEqual(warnings, [])
        self.assertIn('15元纸盒包装券', msg)
        last_call = session.get.call_args_list[-1]
        self.assertEqual(last_call.kwargs['params']['couponId'], 102)

    @patch('main.requests.Session')
    def test_seventh_day_box_limit_reached_claims_fpc(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        coupon_list = [
            make_coupon_item(102, '15元纸盒包装券', 'BOX', can_exchange=False),
            make_coupon_item(101, '10元FPC优惠券', 'PCB', can_exchange=True, dto_name='FPC软板券'),
        ]
        session.get.side_effect = [
            self._assets_ok(200),
            self._config_not_signed(),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 1, 'gainNum': 0}}),
            make_response(200, {'code': 200, 'success': True, 'data': coupon_list}),
            make_response(200, {'code': 200, 'success': True, 'data': True}),
        ]
        kind, msg, warnings = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertEqual(warnings, [])
        self.assertIn('10元FPC优惠券', msg)
        self.assertEqual(session.get.call_args_list[-1].kwargs['params']['couponId'], 101)

    @patch('main.requests.Session')
    def test_seventh_day_all_coupons_exhausted_falls_back_to_beans_cleanly(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        coupon_list = [
            make_coupon_item(102, '15元纸盒包装券', 'BOX', can_exchange=False),
            make_coupon_item(101, '10元FPC优惠券', 'FPC', can_exchange=False),
        ]
        session.get.side_effect = [
            self._assets_ok(200),
            self._config_not_signed(),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 1, 'gainNum': 0}}),
            make_response(200, {'code': 200, 'success': True, 'data': coupon_list}),
            make_response(200, {'code': 200, 'success': True, 'data': {'gainNum': 8}}),
        ]
        kind, msg, warnings = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertEqual(warnings, [])
        self.assertIn('纸盒/FPC券本月已达上限或已兑完', msg)
        self.assertIn('获取8个金豆', msg)
        self.assertIn('当前总数：208', msg)

    @patch('main.requests.Session')
    def test_seventh_day_voucher_returns_scalar_int_succeeds(self, mock_session_cls):
        """测试 receiveVoucher 接口返回 data 为标量数字（如 8）时不会报 'int' object has no attribute 'get'"""
        session = mock_session_cls.return_value.__enter__.return_value
        coupon_list = [
            make_coupon_item(102, '15元纸盒包装券', 'BOX', can_exchange=False),
            make_coupon_item(101, '10元FPC优惠券', 'FPC', can_exchange=False),
        ]
        session.get.side_effect = [
            self._assets_ok(200),
            self._config_not_signed(),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 1, 'gainNum': 0}}),
            make_response(200, {'code': 200, 'success': True, 'data': coupon_list}),
            make_response(200, {'code': 200, 'success': True, 'data': 8}),
        ]
        kind, msg, warnings = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertEqual(warnings, [])
        self.assertIn('获取8个金豆', msg)
        self.assertIn('当前总数：208', msg)

    @patch('main.requests.Session')
    def test_seventh_day_unmatched_coupon_names_emits_warning_and_falls_back(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        coupon_list = [
            make_coupon_item(201, '神秘新品券', 'CNC', can_exchange=True),
        ]
        session.get.side_effect = [
            self._assets_ok(200),
            self._config_not_signed(),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 1, 'gainNum': 0}}),
            make_response(200, {'code': 200, 'success': True, 'data': coupon_list}),
            make_response(200, {'code': 200, 'success': True, 'data': {'gainNum': 8}}),
        ]
        kind, msg, warnings = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertEqual(len(warnings), 1)
        self.assertIn('可能官方改名', warnings[0])
        self.assertIn('神秘新品券', warnings[0])
        self.assertIn('当前总数：208', msg)

    @patch('main.requests.Session')
    def test_seventh_day_coupon_list_error_emits_warning_and_falls_back(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.side_effect = [
            self._assets_ok(50),
            make_response(
                200,
                {'code': 200, 'success': True, 'data': {'haveSignIn': True, 'haveReceive': False}},
            ),
            make_response(500, text='server error'),
            make_response(200, {'code': 200, 'success': True, 'data': {'gainNum': 8}}),
        ]
        kind, msg, warnings = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertEqual(len(warnings), 1)
        self.assertIn('查询券列表请求失败', warnings[0])
        self.assertIn('当前总数：58', msg)

    @patch('main.requests.Session')
    def test_seventh_day_receive_coupon_terminal_retry_and_fallback(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        coupon_list = [
            make_coupon_item(102, '纸盒券', 'BOX', can_exchange=True),
        ]
        session.get.side_effect = [
            self._assets_ok(50),
            self._config_not_signed(),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 1, 'gainNum': 0}}),
            make_response(200, {'code': 200, 'success': True, 'data': coupon_list}),
            make_response(200, {'code': 500, 'success': False, 'message': 'terminal参数错误'}),
            make_response(200, {'code': 200, 'success': True, 'data': True}),
        ]
        kind, msg, warnings = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertEqual(warnings, [])
        self.assertIn('纸盒券', msg)
        self.assertEqual(session.get.call_args_list[-1].kwargs['params']['terminal'], 'WEB')

    @patch('main.requests.Session')
    def test_seventh_day_receive_coupon_and_voucher_both_fail(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        coupon_list = [
            make_coupon_item(102, '纸盒券', 'BOX', can_exchange=True),
        ]
        session.get.side_effect = [
            self._assets_ok(50),
            self._config_not_signed(),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 1, 'gainNum': 0}}),
            make_response(200, {'code': 200, 'success': True, 'data': coupon_list}),
            make_response(200, {'code': 500, 'success': False, 'message': '库存不足'}),
            make_response(200, {'code': 500, 'success': False, 'message': '金豆通道维护'}),
        ]
        kind, msg, warnings = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'failed')
        self.assertEqual(len(warnings), 2)
        self.assertIn('库存不足', msg)
        self.assertIn('金豆通道维护', msg)

    @patch('main.requests.Session')
    def test_sign_in_already_signed_via_config(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.side_effect = [
            self._assets_ok(120),
            make_response(
                200,
                {'code': 200, 'success': True, 'data': {'haveSignIn': True, 'haveReceive': True}},
            ),
        ]
        kind, msg, warnings = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'already_signed')
        self.assertEqual(warnings, [])
        self.assertIn('今日已签到', msg)
        self.assertEqual(session.get.call_count, 2)

    @patch('main.requests.Session')
    def test_preview_coupons_attaches_list_on_already_signed(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        coupon_list = [
            make_coupon_item(102, '纸盒券', 'BOX', can_exchange=True),
        ]
        session.get.side_effect = [
            self._assets_ok(120),
            make_response(
                200,
                {'code': 200, 'success': True, 'data': {'haveSignIn': True, 'haveReceive': True}},
            ),
            make_response(200, {'code': 200, 'success': True, 'data': coupon_list}),
        ]
        with patch.object(main, 'PREVIEW_COUPONS', True):
            kind, msg, warnings = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'already_signed')
        self.assertEqual(warnings, [])
        self.assertIn('第7天券列表预览', msg)
        self.assertIn('纸盒券[id=102,biz=BOX,可领]', msg)

    @patch('main.requests.Session')
    def test_sign_in_already_signed_via_status_2(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.side_effect = [
            self._assets_ok(120),
            make_response(500, text='config error'),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 2, 'gainNum': 0}}),
        ]
        kind, msg, _ = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'already_signed')
        self.assertIn('今日已签到', msg)

    @patch('main.requests.Session')
    def test_sign_in_401_fails_fast_without_retry(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.return_value = make_response(401, text='<html>404</html>')
        kind, msg, _ = main.sign_in('expired_token_1234', retries=3, backoff=0)
        self.assertEqual(kind, 'failed')
        self.assertIn('Token 已失效', msg)
        self.assertEqual(session.get.call_count, 1)

    @patch('main.requests.Session')
    def test_sign_in_401_json_code_without_success_fails_fast(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.return_value = make_response(
            200, {'code': 401, 'message': '登录已过期', 'data': {}}
        )
        kind, msg, _ = main.sign_in('expired_token_5678', retries=3, backoff=0)
        self.assertEqual(kind, 'failed')
        self.assertIn('Token 已失效', msg)
        self.assertEqual(session.get.call_count, 1)

    @patch('main.requests.Session')
    def test_sign_in_transient_failure_retries_then_succeeds(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.side_effect = [
            requests.Timeout('timed out'),
            self._assets_ok(10),
            self._config_not_signed(),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 1, 'gainNum': 2}}),
        ]
        kind, msg, _ = main.sign_in('valid_token', retries=2, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertIn('当前总数：12', msg)


class TestMainFlow(unittest.TestCase):
    @patch('main.send_msg_by_bark')
    def test_main_missing_token_sends_bark_and_returns_1(self, mock_bark):
        mock_bark.return_value = {'code': 200}
        with patch.object(main, 'TOKEN_LIST', ''), patch.object(main, 'SEND_KEY_LIST', 'k1'):
            code = main.main()
        self.assertEqual(code, 1)
        mock_bark.assert_called_once()

    @patch('main.send_msg_by_bark')
    @patch('main.sign_in')
    def test_main_failure_sends_alert_and_returns_1(self, mock_sign, mock_bark):
        mock_sign.side_effect = [
            ('gained', '✅ 账号(12****78)：获取3个金豆，当前总数：103', []),
            ('failed', '❌ 账号(ab****cd)：Token 已失效', []),
        ]
        mock_bark.return_value = {'code': 200}
        with (
            patch.object(main, 'TOKEN_LIST', 't1,t2'),
            patch.object(main, 'SEND_KEY_LIST', 'bark_key_1'),
            patch.object(main, 'NOTIFY_ALWAYS', False),
            patch('main.time.sleep'),
        ):
            code = main.main()

        self.assertEqual(code, 1)
        mock_bark.assert_called_once()
        _, title, body = mock_bark.call_args[0]
        self.assertIn('异常', title)
        self.assertIn('获取3个金豆', body)
        self.assertIn('Token 已失效', body)

    @patch('main.send_msg_by_bark')
    @patch('main.sign_in')
    def test_main_seventh_day_warning_sends_alert_title_and_returns_0(self, mock_sign, mock_bark):
        mock_sign.return_value = (
            'gained',
            '🎉 账号(12****78)：第七天签到成功，获取8个金豆\n⚠️ 第7天诊断信息：未匹配到目标券',
            ['未匹配到目标券'],
        )
        mock_bark.return_value = {'code': 200}
        with (
            patch.object(main, 'TOKEN_LIST', 't1'),
            patch.object(main, 'SEND_KEY_LIST', 'bark_key_1'),
            patch.object(main, 'NOTIFY_ALWAYS', False),
        ):
            code = main.main()

        self.assertEqual(code, 0)
        mock_bark.assert_called_once()
        _, title, body = mock_bark.call_args[0]
        self.assertIn('第7天告警', title)
        self.assertIn('未匹配到目标券', body)

    @patch('main.send_msg_by_bark')
    @patch('main.sign_in')
    def test_main_already_signed_respects_notify_always(self, mock_sign, mock_bark):
        mock_sign.return_value = ('already_signed', 'ℹ️ 账号(12****78)：今日已签到，当前金豆：100', [])
        mock_bark.return_value = {'code': 200}

        with (
            patch.object(main, 'TOKEN_LIST', 't1'),
            patch.object(main, 'SEND_KEY_LIST', 'bark_key_1'),
            patch.object(main, 'NOTIFY_ALWAYS', False),
            patch.object(main, 'PREVIEW_COUPONS', False),
        ):
            self.assertEqual(main.main(), 0)
        mock_bark.assert_not_called()

        with (
            patch.object(main, 'TOKEN_LIST', 't1'),
            patch.object(main, 'SEND_KEY_LIST', 'bark_key_1'),
            patch.object(main, 'NOTIFY_ALWAYS', True),
            patch.object(main, 'PREVIEW_COUPONS', False),
        ):
            self.assertEqual(main.main(), 0)
        mock_bark.assert_called_once()


if __name__ == '__main__':
    unittest.main()
