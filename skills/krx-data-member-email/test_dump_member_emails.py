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

    def test_waf_html_is_not_proxy_auth(self):
        html = "<html>Access Denied. You are not authorized.</html>"
        self.assertFalse(d.is_proxy_auth_fail(text=html))
        self.assertTrue(d.is_proxy_auth_fail(text="407 Proxy Authentication Required"))
        self.assertTrue(d.is_proxy_auth_fail(text="ip not in white list"))

    def test_exhausted_api(self):
        self.assertTrue(d.extract_is_dead('{"code":"-1","msg":"提取次数已用完"}'))
        self.assertTrue(d.extract_is_dead("余额不足"))
        self.assertFalse(d.extract_is_dead("1.2.3.4:80"))

    def test_count_rewrite(self):
        u = "http://x/api?secret=a&orderNo=b&count=6&isTxt=1"
        self.assertIn("count=2", d.with_extract_count(u, 2))
        self.assertIn("count=6", u)

    def test_host_port_user_pass(self):
        self.assertEqual(
            d.parse_proxy_line("proxy.ipdeep.com:7085:user:pa:ss"),
            "http://user:pa:ss@proxy.ipdeep.com:7085",
        )

    def test_slash_socks5(self):
        self.assertEqual(
            d.parse_proxy_line("210.223.226.182/5588/bee68/6566"),
            "socks5h://bee68:6566@210.223.226.182:5588",
        )

    def test_sync_drops_removed_static(self):
        pool = d.ProxyPool(urls=[])
        gone = "socks5h://aa:aa@1.1.1.1:5588"
        keep = "socks5h://aa:aa@2.2.2.2:5588"
        pool.static_list = [gone, keep]
        pool.static_set = {gone, keep}
        pool.good = [gone, keep]
        orig = d.static_proxy_list
        d.static_proxy_list = lambda: [keep]
        try:
            pool.sync_static()
        finally:
            d.static_proxy_list = orig
        self.assertNotIn(gone, pool.static_list)
        self.assertIn(keep, pool.static_list)


class PoolTests(unittest.TestCase):
    def test_ttl_drops_old_ip(self):
        pool = d.ProxyPool(urls=[])
        pool.use_direct = False
        pool.allow_direct = False
        p = "http://1.2.3.4:80"
        pool.proxies = [p]
        pool.good = [p]
        pool.born[p] = time.time() - d.PROXY_TTL - 1
        self.assertEqual(pool.live_n(), 0)
        pool._expire_old()
        self.assertNotIn(p, pool.born)
        self.assertNotIn(p, pool.good)
        self.assertNotIn(p, pool.proxies)

    def test_prune_caps_bad_and_drops_expired(self):
        pool = d.ProxyPool(urls=[])
        old = "http://1.1.1.1:1"
        new = "http://2.2.2.2:2"
        pool.proxies = [old, new]
        pool.good = [old, new]
        pool.born = {old: time.time() - d.PROXY_TTL - 1, new: time.time()}
        pool.bad = {f"http://9.9.9.{i}:1" for i in range(d.BAD_CAP + 50)}
        pool.prune()
        self.assertNotIn(old, pool.born)
        self.assertNotIn(old, pool.good)
        self.assertIn(new, pool.good)
        self.assertLessEqual(len(pool.bad), d.BAD_CAP)

    def test_pick_skips_direct_when_enough_good(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = True
        for i in range(5):
            p = f"http://1.1.1.{i}:80"
            pool.good.append(p)
            pool.born[p] = time.time()
        seen = {pool.pick() for _ in range(30)}
        self.assertNotIn(d.DIRECT, seen)

    def test_pick_uses_direct_when_no_good(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = True
        self.assertEqual(pool.pick(), d.DIRECT)

    def test_pick_none_during_direct_cooldown(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = True
        pool.fail(d.DIRECT)
        self.assertIsNone(pool.pick())

    def test_direct_cooldown(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = True
        pool.fail(d.DIRECT)
        self.assertGreater(pool.direct_until, time.time())

    def test_static_survives_ttl_and_is_picked(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = False
        p = "http://u:p@proxy.ipdeep.com:7085"
        pool.static_list = [p]
        pool.static_set = {p}
        pool.good = [p]
        pool.born[p] = time.time() - d.PROXY_TTL - 10
        pool._expire_old()
        self.assertIn(p, pool.born)
        self.assertIn(p, pool.good)
        self.assertEqual(pool.pick(), p)

    def test_panda_good_n_ignores_static(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        st = "http://u:p@proxy.ipdeep.com:7085"
        pd = "http://1.2.3.4:80"
        pool.static_list = [st]
        pool.static_set = {st}
        pool.good = [st, pd]
        pool.born[st] = time.time()
        pool.born[pd] = time.time()
        self.assertEqual(pool.panda_good_n(), 1)
        self.assertEqual(pool.panda_proven_n(), 0)
        pool.ok(pd)
        self.assertEqual(pool.panda_proven_n(), 1)

    def test_static_cooldown_skips_pick(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = False
        p = "http://u:p@proxy.ipdeep.com:7085"
        pool.static_list = [p]
        pool.static_set = {p}
        pool.good = [p]
        for _ in range(d.STATIC_STRIKES):
            pool.fail(p)
        self.assertIsNone(pool.pick())

    def test_static_one_waf_stays_pickable(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = False
        p = "http://u:p@proxy.ipdeep.com:7085"
        pool.static_list = [p]
        pool.static_set = {p}
        pool.good = [p]
        pool.fail(p)
        self.assertEqual(pool.pick(), p)
        self.assertNotIn(p, pool.static_down)

    def test_pick_caps_in_flight(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = False
        p = "http://1.2.3.4:80"
        pool.good = [p]
        pool.born[p] = time.time() - 9
        pool.ok(p)
        a = pool.pick()
        b = pool.pick()
        c = pool.pick()
        self.assertEqual({a, b}, {p})
        self.assertIsNone(c)

    def test_new_panda_gets_full_cap(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = False
        p = "http://1.2.3.4:80"
        pool.good = [p]
        pool.born[p] = time.time()
        self.assertEqual(pool.pick(), p)
        self.assertEqual(pool.pick(), p)
        self.assertIsNone(pool.pick())

    def test_pick_uses_all_panda_not_four_trials(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = False
        proven = "http://9.9.9.9:80"
        pool.good = [proven]
        pool.born[proven] = time.time() - 9
        pool.ok(proven)
        pandas = []
        for i in range(8):
            p = f"http://1.1.1.{i}:80"
            pool.good.append(p)
            pool.born[p] = time.time()
            pandas.append(p)
        seen = [pool.pick() for _ in range(2 + 8)]
        self.assertEqual(seen.count(proven), 2)
        self.assertEqual(len([x for x in seen if x in pandas]), 8)

    def test_reap_hung_frees_slot_for_panda(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = False
        proven = "http://9.9.9.9:80"
        panda = "http://1.1.1.1:80"
        pool.good = [proven, panda]
        pool.born[proven] = time.time() - 9
        pool.born[panda] = time.time()
        pool.ok(proven)
        pool.in_flight[proven] = 2
        pool.hold_t[proven] = time.time() - (d.CONNECT_TIMEOUT + d.HTTP_TIMEOUT + 3)
        got = pool.pick()
        self.assertEqual(got, panda)
        self.assertNotIn(proven, pool.proven)

    def test_static_down_skips_after_fail(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = False
        p = "http://u:p@proxy.ipdeep.com:7085"
        pool.static_list = [p]
        pool.static_set = {p}
        pool.good = [p]
        for _ in range(d.STATIC_STRIKES):
            pool.fail(p)
        pool.static_until[p] = 0
        self.assertIsNone(pool.pick())

    def test_static_auth_cools_down_not_dead(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = False
        p = "http://u:p@proxy.ipdeep.com:7085"
        pool.static_list = [p]
        pool.static_set = {p}
        pool.fail(p, auth=True)
        self.assertNotIn(p, pool.bad)
        self.assertIsNone(pool.pick())


class SpeedTests(unittest.TestCase):
    def test_http_timeout_is_connect_read(self):
        self.assertEqual(d.http_timeout(), (d.CONNECT_TIMEOUT, d.HTTP_TIMEOUT))

    def test_pick_spreads_across_exits(self):
        pool = d.ProxyPool(urls=["http://example/x"])
        pool.use_direct = False
        a, b = "http://1.1.1.1:80", "http://2.2.2.2:80"
        pool.good = [a, b]
        pool.born[a] = pool.born[b] = time.time()
        seen = [pool.pick(), pool.pick()]
        self.assertEqual(set(seen), {a, b})

    def test_window_rate_uses_recent_only(self):
        d._recent.clear()
        now = 1_000_000.0
        d._recent.append(now - 90)
        d._recent.append(now - 2)
        d._recent.append(now - 1)
        d._recent.append(now)
        rate = d.window_rate(now, 30.0)
        self.assertGreater(rate, 0.5)
        self.assertLess(rate, 3.0)

    def test_inflight_releases_before_slowest(self):
        order = []

        def work(n):
            time.sleep(0.12 if n == 1 else 0.01)
            return n

        t0 = time.time()
        first_at = None
        with d.ThreadPoolExecutor(max_workers=3) as ex:
            for fut in d.iter_inflight(ex, [1, 2, 3, 4], lambda e, n: e.submit(work, n), 3):
                order.append(fut.result())
                if first_at is None:
                    first_at = time.time() - t0
        self.assertEqual(sorted(order), [1, 2, 3, 4])
        self.assertLess(first_at, 0.1)


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


class TimeoutSessionTests(unittest.TestCase):
    def setUp(self):
        d._tls.cache = {}
        d._tls.proxy = None
        self.pool = d.init_pool([])
        self.pool.use_direct = False

    def tearDown(self):
        d.POOL = None
        d._tls.cache = {}
        d._tls.proxy = None

    def test_http_timeout_no_args_stays_warm(self):
        self.assertEqual(d.http_timeout(), (d.CONNECT_TIMEOUT, d.HTTP_TIMEOUT))

    def test_unproven_panda_uses_short_timeout(self):
        p = "http://1.2.3.4:80"
        self.pool.good = [p]
        self.pool.born[p] = time.time()
        self.assertEqual(d.http_timeout(p), (d.UNPROVEN_CONNECT, d.UNPROVEN_READ))
        self.assertFalse(d.is_warm_proxy(p))

    def test_proven_and_static_use_warm_timeout(self):
        p = "http://1.2.3.4:80"
        self.pool.good = [p]
        self.pool.born[p] = time.time()
        self.pool.ok(p)
        self.assertEqual(d.http_timeout(p), (d.CONNECT_TIMEOUT, d.HTTP_TIMEOUT))
        st = "socks5h://aa:aa@10.0.0.1:5588"
        self.pool.static_set.add(st)
        self.assertTrue(d.is_warm_proxy(st))
        self.assertEqual(d.http_timeout(st), (d.CONNECT_TIMEOUT, d.HTTP_TIMEOUT))

    def test_session_close_for_unproven_keepalive_for_proven(self):
        p = "http://1.2.3.4:80"
        self.pool.good = [p]
        self.pool.born[p] = time.time()
        s0 = d.session_for(p)
        self.assertEqual(s0.headers.get("Connection"), "close")
        self.pool.ok(p)
        d.drop_session(p)
        s1 = d.session_for(p)
        self.assertEqual(s1.headers.get("Connection"), "keep-alive")
        self.assertIsNot(s0, s1)

    def test_drop_session_clears_both_keys_keeps_sticky_proxy(self):
        p = "http://1.2.3.4:80"
        d._tls.proxy = p
        d.session_for(p, keep_alive=False)
        d.session_for(p, keep_alive=True)
        d.drop_session(p)
        self.assertEqual(d._tls.proxy, p)
        self.assertFalse(getattr(d._tls, "cache", {}))
        d.drop_session(p, unstick=True)
        self.assertIsNone(d._tls.proxy)

    def test_unproven_hung_reaps_sooner_than_proven(self):
        raw = "http://1.1.1.1:80"
        proven = "http://9.9.9.9:80"
        self.pool.good = [raw, proven]
        self.pool.born[raw] = self.pool.born[proven] = time.time()
        self.pool.ok(proven)
        self.pool.in_flight[raw] = 1
        self.pool.in_flight[proven] = 1
        self.pool.hold_t[raw] = time.time() - (d.UNPROVEN_CONNECT + d.UNPROVEN_READ + 3)
        self.pool.hold_t[proven] = time.time() - (d.UNPROVEN_CONNECT + d.UNPROVEN_READ + 3)
        self.pool._reap_hung()
        self.assertNotIn(raw, self.pool.in_flight)
        self.assertIn(proven, self.pool.in_flight)


class FollowJobTests(unittest.TestCase):
    def test_idor_with_id_queues_primary_only(self):
        self.assertEqual(d.follow_jobs(("idor", 1, "abc")), [("mail", 1, "abc", "primary")])
        self.assertEqual(d.follow_jobs(("idor", 1, None)), [])

    def test_primary_miss_queues_extra_hit_does_not(self):
        self.assertEqual(
            d.follow_jobs(("mail", 1, "abc", None, "primary")),
            [("mail", 1, "abc", "extra")],
        )
        self.assertEqual(d.follow_jobs(("mail", 1, "abc", "abc@naver.com", "primary")), [])
        self.assertEqual(d.follow_jobs(("mail", 1, "abc", None, "extra")), [])

    def test_drain_followups_jump_the_queue(self):
        seen = []

        def work(job):
            return job

        def on_done(result):
            seen.append(result)
            if result == "a":
                return ["b"]
            return []

        with d.ThreadPoolExecutor(max_workers=1) as ex:
            d.drain_jobs(ex, ["a", "c"], lambda e, x: e.submit(work, x), on_done, 1)
        self.assertEqual(seen, ["a", "b", "c"])

    def test_one_idor_does_not_probe_email(self):
        calls = []

        def fake_post(url, data, need):
            calls.append((url, data, need))
            return {"block1": [{"MBR_ID": "ryujt"}]}

        orig = d.post_ok
        d.post_ok = fake_post
        try:
            self.assertEqual(d.one_idor(2000008331), (2000008331, "ryujt"))
        finally:
            d.post_ok = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], d.IDOR)
        self.assertNotIn("email", calls[0][1])

    def test_one_idor_empty_is_not_nomail(self):
        orig = d.post_ok
        d.post_ok = lambda url, data, need: {"block1": []}
        try:
            self.assertEqual(d.one_idor(1999999999), (1999999999, None))
        finally:
            d.post_ok = orig

    def test_run_job_idor_and_mail_shapes(self):
        orig_idor = d.one_idor
        orig_mail = d.one_mail
        d.one_idor = lambda mno: (mno, "abc")
        d.one_mail = lambda mno, mid, phase="extra": (mno, mid, None if phase == "primary" else "x@nate.com")
        try:
            self.assertEqual(d.run_job(("idor", 9)), ("idor", 9, "abc"))
            self.assertEqual(d.run_job(("mail", 9, "abc", "primary")), ("mail", 9, "abc", None, "primary"))
            self.assertEqual(
                d.run_job(("mail", 9, "abc", "extra")),
                ("mail", 9, "abc", "x@nate.com", "extra"),
            )
        finally:
            d.one_idor = orig_idor
            d.one_mail = orig_mail


class DupEmailTests(unittest.TestCase):
    def test_naver_hit_does_not_touch_rest(self):
        calls = []

        def fake_post(url, data, need):
            calls.append(data["email"])
            return {"isDupMbrEmail": True}

        orig = d.post_ok
        d.post_ok = fake_post
        try:
            self.assertEqual(d.dup_email("ryujt", ["naver.com", "gmail.com"]), "ryujt@naver.com")
        finally:
            d.post_ok = orig
        self.assertEqual(calls, ["ryujt@naver.com"])

    def test_rest_domains_run_after_naver_miss(self):
        calls = []
        lock = __import__("threading").Lock()

        def fake_post(url, data, need):
            with lock:
                calls.append(data["email"])
            return {"isDupMbrEmail": data["email"].endswith("@gmail.com")}

        orig = d.post_ok
        orig_par = d.DOMAIN_PARALLEL
        d.post_ok = fake_post
        d.DOMAIN_PARALLEL = 3
        try:
            self.assertEqual(
                d.dup_email("x", ["naver.com", "gmail.com", "hanmail.net"]),
                "x@gmail.com",
            )
        finally:
            d.post_ok = orig
            d.DOMAIN_PARALLEL = orig_par
        self.assertIn("x@naver.com", calls)
        self.assertIn("x@gmail.com", calls)
        self.assertEqual(calls[0], "x@naver.com")

    def test_all_miss_returns_none(self):
        orig = d.post_ok
        orig_par = d.DOMAIN_PARALLEL
        d.post_ok = lambda url, data, need: {"isDupMbrEmail": False}
        d.DOMAIN_PARALLEL = 3
        try:
            self.assertIsNone(d.dup_email("x", ["naver.com", "gmail.com"]))
        finally:
            d.post_ok = orig
            d.DOMAIN_PARALLEL = orig_par

    def test_one_mail_primary_uses_primary_domains(self):
        seen = []

        def fake_dup(mid, domains):
            seen.append(list(domains))
            return None

        orig = d.dup_email
        d.dup_email = fake_dup
        try:
            self.assertEqual(d.one_mail(1, "abc", "primary"), (1, "abc", None))
            self.assertEqual(d.one_mail(1, "abc", "extra"), (1, "abc", None))
        finally:
            d.dup_email = orig
        self.assertEqual(seen[0], d.DOMS_PRIMARY)
        self.assertEqual(seen[1], d.DOMS_EXTRA)


if __name__ == "__main__":
    unittest.main()
