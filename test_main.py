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
        kind, msg = main.sign_in('valid_token', retries=2, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertIn('获取4个金豆', msg)
        self.assertIn('当前总数：104', msg)

    @patch('main.requests.Session')
    def test_sign_in_seventh_day_success(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.side_effect = [
            self._assets_ok(200),
            self._config_not_signed(),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 1, 'gainNum': 0}}),
            make_response(200, {'code': 200, 'success': True, 'data': {'gainNum': 8}}),
        ]
        kind, msg = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertIn('第七天签到成功', msg)
        self.assertIn('当前总数：208', msg)

    @patch('main.requests.Session')
    def test_sign_in_config_unclaimed_seventh_day(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.side_effect = [
            self._assets_ok(50),
            make_response(
                200,
                {'code': 200, 'success': True, 'data': {'haveSignIn': True, 'haveReceive': False}},
            ),
            make_response(200, {'code': 200, 'success': True, 'data': {'gainNum': 10}}),
        ]
        kind, msg = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'gained')
        self.assertIn('获取10个金豆', msg)
        self.assertIn('当前总数：60', msg)

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
        kind, msg = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'already_signed')
        self.assertIn('今日已签到', msg)
        self.assertEqual(session.get.call_count, 2)

    @patch('main.requests.Session')
    def test_sign_in_already_signed_via_status_2(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.side_effect = [
            self._assets_ok(120),
            make_response(500, text='config error'),
            make_response(200, {'code': 200, 'success': True, 'data': {'status': 2, 'gainNum': 0}}),
        ]
        kind, msg = main.sign_in('valid_token', retries=1, backoff=0)
        self.assertEqual(kind, 'already_signed')
        self.assertIn('今日已签到', msg)

    @patch('main.requests.Session')
    def test_sign_in_401_fails_fast_without_retry(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.return_value = make_response(401, text='<html>404</html>')
        kind, msg = main.sign_in('expired_token_1234', retries=3, backoff=0)
        self.assertEqual(kind, 'failed')
        self.assertIn('Token 已失效', msg)
        self.assertEqual(session.get.call_count, 1)

    @patch('main.requests.Session')
    def test_sign_in_401_json_code_without_success_fails_fast(self, mock_session_cls):
        session = mock_session_cls.return_value.__enter__.return_value
        session.get.return_value = make_response(
            200, {'code': 401, 'message': '登录已过期', 'data': {}}
        )
        kind, msg = main.sign_in('expired_token_5678', retries=3, backoff=0)
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
        kind, msg = main.sign_in('valid_token', retries=2, backoff=0)
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
            ('gained', '✅ 账号(12****78)：获取3个金豆，当前总数：103'),
            ('failed', '❌ 账号(ab****cd)：Token 已失效'),
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
    def test_main_already_signed_respects_notify_always(self, mock_sign, mock_bark):
        mock_sign.return_value = ('already_signed', 'ℹ️ 账号(12****78)：今日已签到，当前金豆：100')
        mock_bark.return_value = {'code': 200}

        with (
            patch.object(main, 'TOKEN_LIST', 't1'),
            patch.object(main, 'SEND_KEY_LIST', 'bark_key_1'),
            patch.object(main, 'NOTIFY_ALWAYS', False),
        ):
            self.assertEqual(main.main(), 0)
        mock_bark.assert_not_called()

        with (
            patch.object(main, 'TOKEN_LIST', 't1'),
            patch.object(main, 'SEND_KEY_LIST', 'bark_key_1'),
            patch.object(main, 'NOTIFY_ALWAYS', True),
        ):
            self.assertEqual(main.main(), 0)
        mock_bark.assert_called_once()


if __name__ == '__main__':
    unittest.main()
