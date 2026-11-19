import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from nacl import encoding, public

import update_jlc_token as updater


class TestTokenHelpers(unittest.TestCase):
    def test_unwrap_storage_token(self):
        self.assertIsNone(updater.unwrap_storage_token(None))
        self.assertIsNone(updater.unwrap_storage_token(''))
        self.assertIsNone(updater.unwrap_storage_token('NONE'))
        self.assertEqual(updater.unwrap_storage_token('abc-123-token'), 'abc-123-token')
        self.assertEqual(updater.unwrap_storage_token('"abc-123-token"'), 'abc-123-token')
        self.assertEqual(
            updater.unwrap_storage_token('{"type":"string","data":"wrapped-token-999"}'),
            'wrapped-token-999',
        )
        self.assertIsNone(updater.unwrap_storage_token('{"type":"string","data":"NONE"}'))

    def test_extract_github_token_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cred_file = Path(tmpdir) / '.git-credentials'
            cred_file.write_text('https://tthking:ghp_testtoken123@github.com\n', encoding='utf-8')
            with patch.dict('os.environ', {}, clear=True):
                self.assertEqual(updater.extract_github_token(cred_file), 'ghp_testtoken123')

    def test_encrypt_secret_roundtrip(self):
        priv = public.PrivateKey.generate()
        pub_b64 = priv.public_key.encode(encoding.Base64Encoder()).decode('utf-8')
        cipher_b64 = updater.encrypt_secret(pub_b64, 'my-jlc-secret-token')
        decrypted = public.SealedBox(priv).decrypt(base64.b64decode(cipher_b64)).decode('utf-8')
        self.assertEqual(decrypted, 'my-jlc-secret-token')


class TestApiCalls(unittest.TestCase):
    @patch('update_jlc_token.requests.get')
    def test_validate_jlc_token_valid_and_invalid(self, mock_get):
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {
            'code': 200,
            'success': True,
            'data': {'customerCode': '1122338A', 'integralVoucher': 459},
        }
        mock_get.return_value = ok_resp
        ok, code, beans = updater.validate_jlc_token('good_tok')
        self.assertTrue(ok)
        self.assertEqual(code, '1122338A')
        self.assertEqual(beans, 459)

        bad_resp = MagicMock()
        bad_resp.status_code = 401
        mock_get.return_value = bad_resp
        ok, code, beans = updater.validate_jlc_token('bad_tok')
        self.assertFalse(ok)
        self.assertIsNone(code)

    @patch('update_jlc_token.requests.put')
    @patch('update_jlc_token.requests.get')
    def test_update_github_secret(self, mock_get, mock_put):
        priv = public.PrivateKey.generate()
        pub_b64 = priv.public_key.encode(encoding.Base64Encoder()).decode('utf-8')

        pk_resp = MagicMock()
        pk_resp.json.return_value = {'key_id': 'kid-1', 'key': pub_b64}
        pk_resp.raise_for_status.return_value = None
        mock_get.return_value = pk_resp

        put_resp = MagicMock()
        put_resp.raise_for_status.return_value = None
        mock_put.return_value = put_resp

        updater.update_github_secret('ghp_abc', 'new_jlc_tok')
        mock_put.assert_called_once()
        payload = mock_put.call_args.kwargs['json']
        self.assertEqual(payload['key_id'], 'kid-1')
        decrypted = public.SealedBox(priv).decrypt(base64.b64decode(payload['encrypted_value'])).decode('utf-8')
        self.assertEqual(decrypted, 'new_jlc_tok')


if __name__ == '__main__':
    unittest.main()
