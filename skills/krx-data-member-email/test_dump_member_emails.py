#!/usr/bin/env python3
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dump_member_emails as d


class ParseTests(unittest.TestCase):
    def test_crlf_txt(self):
        body = "1.2.3.4:80\r\n5.6.7.8:90\r\n"
        self.assertEqual(d.parse_extract_body(body), ["http://1.2.3.4:80", "http://5.6.7.8:90"])

    def test_ip_port_user_pass(self):
        self.assertEqual(
            d.parse_proxy_line("10.0.0.1:8000:user:pa:ss"),
            "http://user:pa:ss@10.0.0.1:8000",
        )

    def test_user_at_host(self):
        self.assertEqual(
            d.parse_proxy_line("u:p@1.1.1.1:9"),
            "http://u:p@1.1.1.1:9",
        )

    def test_json_obj(self):
        body = '{"code":"0","obj":[{"ip":"8.8.8.8","port":"1234"}]}'
        self.assertEqual(d.parse_extract_body(body), ["http://8.8.8.8:1234"])

    def test_json_with_account(self):
        body = '{"obj":[{"ip":"8.8.8.8","port":"1","account":"a","password":"b"}]}'
        self.assertEqual(d.parse_extract_body(body), ["http://a:b@8.8.8.8:1"])

    def test_rate_limit_json(self):
        body = '{"code":"-102","msg":"请按规定1秒提取一次","data":[null]}'
        self.assertEqual(d.parse_extract_body(body), [])

    def test_inject_auth(self):
        self.assertEqual(
            d.parse_proxy_line("1.2.3.4:80", extra_auth="u:p"),
            "http://u:p@1.2.3.4:80",
        )

    def test_waf_html(self):
        self.assertTrue(d.is_waf_text(200, "<html><title>에러페이지</title>"))
        self.assertTrue(d.is_waf_text(200, "<head><title>The document has been moved.</title>"))
        self.assertTrue(d.is_waf_text(403, "{}"))
        self.assertFalse(d.is_waf_text(200, '{"block1":[]}'))

    def test_exhausted_api(self):
        self.assertTrue(d.extract_is_dead('{"code":"-1","msg":"提取次数已用完"}'))
        self.assertTrue(d.extract_is_dead("余额不足"))
        self.assertFalse(d.extract_is_dead("1.2.3.4:80"))

    def test_count_rewrite(self):
        u = "http://x/api?secret=a&orderNo=b&count=6&isTxt=1"
        self.assertIn("count=2", d.with_extract_count(u, 2))


class PoolTests(unittest.TestCase):
    def test_ttl_drops_old_ip(self):
        pool = d.ProxyPool(urls=[])
        pool.allow_direct = False
        p = "http://1.2.3.4:80"
        pool.proxies = [p]
        pool.good = [p]
        pool.born[p] = time.time() - d.PROXY_TTL - 1
        self.assertEqual(pool.live_n(), 0)
        pool._expire_old()
        self.assertIn(p, pool.bad)


class JobTests(unittest.TestCase):
    def test_no_overlap_and_skip(self):
        have_id = {2000006000: "a", 2000009000: "b"}
        have_email = {2000006000: "a@naver.com"}
        scanned = {2000006001: "empty", 2000006000: "id"}
        mail, head, tail, holes = d.build_jobs(
            2000005000, 2000009005, have_id, have_email, scanned, 2000005966
        )
        self.assertEqual(mail, [(2000009000, "b")])
        all_idor = head + tail + holes
        self.assertEqual(len(all_idor), len(set(all_idor)))
        self.assertNotIn(2000006000, all_idor)
        self.assertNotIn(2000006001, all_idor)
        self.assertNotIn(2000009000, all_idor)
        self.assertIn(2000005000, head)
        self.assertIn(2000009001, tail)
        self.assertIn(2000005966, holes)

    def test_nomail_not_requeued(self):
        have_id = {1: "x"}
        scanned = {1: "nomail"}
        mail, *_ = d.build_jobs(1, 3, have_id, {}, scanned, 2)
        self.assertEqual(mail, [])


class CsvTests(unittest.TestCase):
    def test_load_map(self):
        with tempfile.NamedTemporaryFile("w", delete=False, newline="") as f:
            f.write("mbrNo,mbrId\n2000008331,ryujt\n")
            path = f.name
        try:
            m = d.load_csv_map(path)
            self.assertEqual(m[2000008331], "ryujt")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
