#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cloudflare Turnstile 交互式验证求解器

原理:
- Turnstile 在 Playwright/patchright 等自动化浏览器中会被静默卡死（CDP 检测），
  组件 shell 渲染了但挑战 iframe 永远不加载
- 但用【真实 Chrome】（无自动化注入）+ CDP 连接时，组件能完整运行
- 交互式勾选框的 UI 渲染在 closed shadow DOM 里，DOM 查询不可见，
  但组件固定 300x65（本地化文案时更宽），勾选框固定在左侧中间，
  用坐标模拟真实鼠标点击即可通过

两种使用方式:
- get_turnstile_token(sitekey): 本地页面渲染组件，仅返回 token
- solve_and_checkin(base_url, ...): 在站点页面内完成 取sitekey -> 解验证 -> 签到 全流程，
  适用于 requests 被 CF 拦截的环境（如 GitHub Actions）
"""
import http.server
import json
import os
import socket
import subprocess
import tempfile
import threading
import time

CHROME_CANDIDATES = [
    os.environ.get('TURNSTILE_CHROME_PATH'),
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    '/usr/bin/google-chrome-stable',
    '/usr/bin/google-chrome',
    '/opt/google/chrome/chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
]

LOCAL_HTML = ('<html><head><title>ts</title></head>'
              '<body style="margin:0"><div id="ts" style="padding:10px"></div></body></html>')

RENDER_JS = '''async (sitekey) => {
    if (!document.getElementById('ts')) {
        const holder = document.createElement('div');
        holder.id = 'ts';
        holder.style.cssText = 'position:fixed;top:10px;left:10px;z-index:99999;';
        document.body.appendChild(holder);
    }
    if (!document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]')) {
        const s = document.createElement('script');
        s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
        document.head.appendChild(s);
    }
    for (let i = 0; i < 40; i++) {
        if (window.turnstile && window.turnstile.render) break;
        await new Promise(r => setTimeout(r, 500));
    }
    if (!window.turnstile) throw new Error('turnstile api 初始化失败');
    window.__t = null; window.__e = null; window.__inter = false;
    window.__sitekey = sitekey;
    window.__wid = turnstile.render(document.getElementById('ts'), {
        sitekey: sitekey,
        callback: t => window.__t = t,
        'error-callback': c => window.__e = String(c),
        'before-interactive-callback': () => window.__inter = true,
    });
}'''

RESET_JS = '''() => {
    try { turnstile.remove(window.__wid); } catch (e) {}
    window.__e = null; window.__t = null; window.__inter = false;
    window.__wid = turnstile.render(document.getElementById('ts'), {
        sitekey: window.__sitekey,
        callback: t => window.__t = t,
        'error-callback': c => window.__e = String(c),
        'before-interactive-callback': () => window.__inter = true,
    });
}'''

# 渲染 -> 等交互回调 -> 坐标点击 -> 拿 token；每轮只点一次，失败重置重试
SOLVE_JS_POLL = '''() => ({
    token: window.__t || null,
    err: window.__e,
    interactive: !!window.__inter,
    box: (() => {
        const host = document.querySelector('#ts > div');
        if (!host) return null;
        const r = host.getBoundingClientRect();
        return { x: r.x, y: r.y, w: r.width, h: r.height };
    })(),
})'''


def find_chrome():
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    return None


def _get_system_proxy():
    """读取系统代理（Windows 注册表 / 环境变量），浏览器必须与 requests 走同一出口"""
    try:
        import urllib.request
        proxies = urllib.request.getproxies()
        return proxies.get('https') or proxies.get('http')
    except Exception:
        return None


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _LocalHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(LOCAL_HTML.encode())

    def log_message(self, *a):
        pass


def _launch_chrome(proxy=None, url=None):
    """启动真实 Chrome 并返回 (proc, dbg_port)"""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError('未找到 Chrome 浏览器，无法通过 Turnstile 验证')
    dbg_port = _free_port()
    udd = tempfile.mkdtemp(prefix='ts_solver_')
    chrome_log = tempfile.NamedTemporaryFile(prefix='chrome_', suffix='.log', delete=False)
    args = [
        chrome,
        f'--remote-debugging-port={dbg_port}',
        f'--user-data-dir={udd}',
        '--no-first-run', '--no-default-browser-check',
        '--no-sandbox', '--disable-dev-shm-usage',
        '--disable-gpu',
        '--remote-allow-origins=*',
        '--window-size=700,500',
    ]
    if proxy:
        args.append(f'--proxy-server={proxy}')
    args.append(url or 'about:blank')
    proc = subprocess.Popen(args, stdout=chrome_log, stderr=chrome_log)

    # 轮询等待 CDP 端口就绪（冷启动可能远超 4 秒，固定 sleep 会 ECONNREFUSED）
    import socket as _socket
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            chrome_log.flush()
            with open(chrome_log.name, errors='ignore') as lf:
                tail = lf.read()[-400:]
            raise RuntimeError(f'Chrome 启动失败（退出码 {proc.returncode}）：{tail}')
        try:
            with _socket.create_connection(('127.0.0.1', dbg_port), timeout=2):
                time.sleep(0.5)
                return proc, dbg_port
        except OSError:
            time.sleep(0.5)
    raise RuntimeError('等待 Chrome CDP 端口超时（40s）')


def _solve_in_page(page, sitekey, max_wait=90, verbose=True):
    """在当前页面渲染 Turnstile 并自动点击，返回 token 或 None"""
    page.evaluate(RENDER_JS, sitekey)

    clicked = False
    round_started = 0.0
    rounds = 0
    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(1)
        attempt = int(time.time() - start)
        st = page.evaluate(SOLVE_JS_POLL)

        if st['token']:
            if verbose:
                print(f'[Turnstile] token 获取成功 (耗时 {attempt}s)')
            return st['token']

        # 300xxx = 挑战执行失败（如重复点击破坏挑战），重置组件重试
        if st['err']:
            if verbose:
                print(f'[Turnstile] 挑战失败 ({st["err"]})，重置组件重试...')
            page.evaluate(RESET_JS)
            clicked = False
            rounds += 1
            if rounds >= 4:
                if verbose:
                    print('[Turnstile] 多轮重试均失败')
                return None
            continue

        box, interactive = st['box'], st['interactive']
        # 每轮只点击一次：等交互回调出现并稍作停顿后点击，之后耐心等待结果
        if box and box['w'] > 50 and interactive and not clicked:
            time.sleep(2)
            cx, cy = box['x'] + 30, box['y'] + box['h'] / 2
            if verbose:
                print(f'[Turnstile] 交互验证框出现，模拟点击勾选框 ({cx:.0f}, {cy:.0f})')
            page.mouse.move(cx - 15, cy - 8)
            time.sleep(0.4)
            page.mouse.move(cx - 3, cy)
            time.sleep(0.4)
            page.mouse.click(cx, cy)
            clicked = True
            round_started = time.time()
        elif clicked and time.time() - round_started > 30:
            # 点击后 30s 无结果且无报错，视为挑战卡死，重置重试
            page.evaluate(RESET_JS)
            clicked = False
            rounds += 1
            if rounds >= 4:
                if verbose:
                    print('[Turnstile] 多轮重试均失败')
                return None
    if verbose:
        print('[Turnstile] 等待超时')
    return None


def _wait_cf_pass(page, base_url, timeout=60, verbose=True):
    """导航到站点并等待 CF 验证通过，返回是否成功"""
    page.goto(base_url, wait_until='domcontentloaded', timeout=45000)
    start = time.time()
    while time.time() - start < timeout:
        title = page.title()
        if 'Attention Required' not in title and 'Just a moment' not in title:
            return True
        if verbose:
            print(f'[Turnstile] 等待 CF 验证通过: {title[:40]}')
        time.sleep(5)
    return False


def solve_and_checkin(base_url: str, sitekey: str = None, auth_headers: dict = None,
                      proxy: str = None, max_wait: int = 120, verbose: bool = True,
                      session_cookie: str = None):
    """
    在站点页面内完成 Turnstile 解验证 + 签到全流程（requests 被 CF 拦也能走通）

    Args:
        base_url: 站点地址
        sitekey: Turnstile sitekey，不传则自动从 /api/status 获取
        auth_headers: 签到请求头（Authorization / New-Api-User）
        proxy: 代理地址；默认自动读系统代理（GHA 无代理则为 None）
        max_wait: 最大等待秒数
        verbose: 详细输出
        session_cookie: session cookie(选项,注入后不依赖 Authorization)
    Returns:
        签到接口的 JSON 响应 dict，失败返回 None
    """
    if proxy is None:
        proxy = _get_system_proxy()
    auth_headers = auth_headers or {}

    proc, dbg_port = _launch_chrome(proxy, url=base_url)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = None
            last_err = None
            for _ in range(3):
                try:
                    browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{dbg_port}')
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(3)
            if browser is None:
                raise RuntimeError(f'CDP 连接失败: {last_err}')
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 注入 session cookie(若有),保证签到请求带登录态
            if session_cookie:
                try:
                    domain = base_url.replace('https://', '').replace('http://', '').split('/')[0]
                    ctx.add_cookies([{'name': 'session', 'value': session_cookie,
                                      'domain': domain, 'path': '/'}])
                except Exception as e:
                    if verbose:
                        print(f'[Turnstile] 注入 session cookie 失败: {e}')

            if not _wait_cf_pass(page, base_url, verbose=verbose):
                if verbose:
                    print('[Turnstile] CF 验证未通过')
                return None

            # 关键: /api/user/checkin 是路径级 CF 挑战(与 /api/gwent/* 一样),
            # 必须先导航到该路径触发挑战并等待返回 JSON,否则 POST 会被 CF 拦截
            try:
                page.goto(base_url + '/api/user/checkin', wait_until='domcontentloaded', timeout=45000)
            except Exception:
                pass
            t0_ck = time.time()
            path_ok = False
            while time.time() - t0_ck < 60:
                try:
                    body_ck = page.evaluate("document.body ? document.body.innerText.slice(0,150) : ''")
                except Exception:
                    body_ck = ''
                if body_ck.strip().startswith('{'):
                    path_ok = True
                    if verbose:
                        print('[Turnstile] 签到路径挑战已通过')
                    break
                if time.time() - t0_ck > 10:
                    try:
                        page.reload(wait_until='domcontentloaded', timeout=20000)
                    except Exception:
                        pass
                    t0_reload = time.time()
                    while time.time() - t0_reload < 20:
                        try:
                            body_ck = page.evaluate("document.body ? document.body.innerText.slice(0,150) : ''")
                        except Exception:
                            body_ck = ''
                        if body_ck.strip().startswith('{'):
                            path_ok = True
                            break
                        time.sleep(3)
                    if path_ok:
                        break
                else:
                    time.sleep(4)
            if not path_ok and verbose:
                print('[Turnstile] 签到路径挑战未通过,继续尝试签到')

            # 页面内 JS：PoW 挑战获取 + 求解（SHA-256 前导零比特，与官方 Worker 算法一致）
            SOLVE_POW_JS = '''async (headers) => {
                const r = await fetch('/api/user/pow/challenge?action=checkin', {
                    credentials: 'include', headers: headers
                });
                const j = await r.json();
                if (!j.success) throw new Error(j.message || '获取 PoW 挑战失败');
                const {challenge_id, prefix, difficulty} = j.data;
                let n = 0;
                for (;;) {
                    const nonce = n.toString(16).padStart(8, '0');
                    const h = new Uint8Array(await crypto.subtle.digest('SHA-256',
                        new TextEncoder().encode(prefix + nonce)));
                    const full = Math.floor(difficulty / 8), rem = difficulty % 8;
                    let ok = true;
                    for (let i = 0; i < full; i++) if (h[i] !== 0) { ok = false; break; }
                    if (ok && rem > 0 && (h[full] & (255 << (8 - rem))) !== 0) ok = false;
                    if (ok) return {challenge_id, nonce};
                    n++;
                    if (n > 0xffffffff) throw new Error('超过最大尝试次数');
                }
            }'''

            POST_JS = '''async (args) => {
                const headers = Object.assign({'Content-Type': 'application/json'}, args.headers);
                const resp = await fetch('/api/user/checkin' + (args.query || ''), {
                    method: 'POST',
                    headers: headers,
                    body: JSON.stringify(args.body || {}),
                    credentials: 'include',
                });
                const text = await resp.text();
                try { return JSON.parse(text); }
                catch (e) { return { success: false, message: '响应非JSON: ' + text.substring(0, 120) }; }
            }'''

            # 第一跳：不带验证参数直接签到
            result = page.evaluate(POST_JS, {'headers': auth_headers})

            # 按服务端要求补验证（最多两轮：PoW / Turnstile 任意组合）
            ts_token = None
            pow_params = None
            for _ in range(2):
                if not isinstance(result, dict) or result.get('success'):
                    break
                msg = str(result.get('message', '')).lower()
                if 'pow' in msg and pow_params is None:
                    if verbose:
                        print('[Turnstile] 服务端要求 PoW 工作量证明，页面内解算...')
                    pow_params = page.evaluate(SOLVE_POW_JS, auth_headers)
                    if verbose:
                        print(f"[Turnstile] PoW 解算完成: nonce={pow_params['nonce']}")
                elif 'turnstile' in msg and ts_token is None:
                    if not sitekey:
                        sitekey = page.evaluate('''async () => {
                            try {
                                const r = await fetch('/api/status');
                                const j = await r.json();
                                return (j.data && j.data.turnstile_site_key) || null;
                            } catch (e) { return null; }
                        }''')
                        if verbose:
                            print(f'[Turnstile] sitekey: {sitekey}')
                    if not sitekey:
                        break
                    ts_token = _solve_in_page(page, sitekey, max_wait=max_wait, verbose=verbose)
                    if not ts_token:
                        break
                else:
                    break
                # 组装查询参数并重试
                query = ''
                body = {}
                from urllib.parse import quote
                if ts_token:
                    query += ('&' if query else '?') + 'turnstile=' + quote(ts_token)
                    body['turnstile'] = ts_token
                if pow_params:
                    query += ('&' if query else '?') + ('pow_challenge=' + quote(pow_params['challenge_id']) +
                             '&pow_nonce=' + quote(pow_params['nonce']))
                result = page.evaluate(POST_JS, {'headers': auth_headers, 'query': query, 'body': body})

            browser.close()
            return result
    except Exception as e:
        print(f'[Turnstile] 求解失败: {e}')
        return None
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def get_waf_cookies(base_url: str, session_cookie: str = None, proxy: str = None,
                    max_wait: int = 90, verbose: bool = True):
    """
    使用真实 Chrome 访问站点,执行 JS 通过 WAF(阿里云 ESA/Tengine acw_tc /
    cdn_sec_tc / acw_sc__v2 或网宿类反爬),提取 WAF cookie 字典。

    anyrouter.top / agentrouter.org 等站点启用阿里云 ESA(Tengine)防护:
    requests 直连返回加密混淆 JS 或 HTML,必须先让真实浏览器执行页面 JS
    获得 acw_tc 等 cookie,再用携带这些 cookie 的请求访问 API。

    Args:
        base_url: 站点地址
        session_cookie: 可选,同时注入 session cookie
        proxy: 代理地址
        max_wait: 最大等待秒数
        verbose: 详细输出

    Returns:
        {"acw_tc": "...", "cdn_sec_tc": "...", ...} 或 None(失败)
    """
    if proxy is None:
        proxy = _get_system_proxy()
    proc, dbg_port = _launch_chrome(proxy, url=base_url)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = None
            last_err = None
            for _ in range(3):
                try:
                    browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{dbg_port}')
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(3)
            if browser is None:
                raise RuntimeError(f'CDP 连接失败: {last_err}')
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            if session_cookie:
                try:
                    domain = base_url.replace('https://', '').replace('http://', '').split('/')[0]
                    ctx.add_cookies([{'name': 'session', 'value': session_cookie,
                                      'domain': domain, 'path': '/'}])
                except Exception:
                    pass

            # 导航首页触发 WAF JS(阿里云会 302/set-cookie acw_tc 系列)
            if verbose:
                print(f'[WAF] 导航 {base_url} 执行阿里云 ESA JS...')
            try:
                page.goto(base_url, wait_until='domcontentloaded', timeout=45000)
            except Exception:
                pass

            # 等待页面 JS 完成(阿里云挑战页会自动跳转;最多 max_wait 秒)
            t0 = time.time()
            while time.time() - t0 < max_wait:
                try:
                    title = page.title()
                    body = page.evaluate("document.body ? document.body.innerText.slice(0,80) : ''")
                except Exception:
                    title, body = '', ''
                # 阿里云挑战页相关关键词
                if any(k in body for k in ('验证', 'captcha', '安全', 'verify', 'Access denied',
                                            '请稍候', 'Just a moment', '检查')):
                    if verbose and int(time.time() - t0) % 6 == 0:
                        print(f'[WAF] 等待 JS 通过: {body[:40]!r}')
                    time.sleep(3)
                    continue
                # 页面已渲染出真实内容(标题/正文不再是挑战页)
                break

            # 提取 WAF cookie
            cookies = ctx.cookies()
            waf_cookies = {}
            for c in cookies:
                name = c.get('name', '')
                value = c.get('value', '') or ''
                if name in ('acw_tc', 'cdn_sec_tc', 'acw_sc__v2', 'aliyungf_tc', '__jsluid_s',
                            '_tengate_acw_tc', 'x5sec'):
                    waf_cookies[name] = value
                if name == 'session' and session_cookie:
                    waf_cookies['session'] = value
            ses_cookies = [c for c in cookies if c.get('name') == 'session']
            if session_cookie and not ses_cookies:
                # 注入的 session 也加进去(CDP cookie 可能未回显)
                waf_cookies['session'] = session_cookie

            if verbose:
                if waf_cookies:
                    print(f'[WAF] 获取到 cookies: {list(waf_cookies.keys())}')
                else:
                    print('[WAF] 未获取到 WAF cookies(可能该站无 ESA 防护或挑战未通过)')
            browser.close()
            return waf_cookies if waf_cookies else None
    except Exception as e:
        print(f'[WAF] 获取 WAF cookies 失败: {e}')
        return None
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def get_turnstile_token(sitekey: str, proxy: str = None, max_wait: int = 90, verbose: bool = True):
    """本地页面渲染 Turnstile 组件并自动点击，返回 token（调试用）"""
    if proxy is None:
        proxy = _get_system_proxy()

    port_http = _free_port()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', port_http), _LocalHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    proc = None
    token = None
    try:
        proc, dbg_port = _launch_chrome(proxy, url=f'http://127.0.0.1:{port_http}/')
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = None
            last_err = None
            for _ in range(3):
                try:
                    browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{dbg_port}')
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(3)
            if browser is None:
                raise RuntimeError(f'CDP 连接失败: {last_err}')
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            token = _solve_in_page(page, sitekey, max_wait=max_wait, verbose=verbose)
            browser.close()
    except Exception as e:
        print(f'[Turnstile] 求解失败: {e}')
    finally:
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        server.shutdown()
    return token


def solve_and_api(base_url: str, session_cookie: str = None, auth_headers: dict = None,
                  proxy: str = None, max_wait: int = 90, verbose: bool = True,
                  path: str = '/api/user/self', method: str = 'GET'):
    """
    真实 Chrome 过 WAF/CF 后,在页面内执行指定 API 请求。

    用于 aliyun_waf_aa/bb 这类新版阿里云 WAF 站点(agentrouter.org 等):
    requests 即使携带 acw_tc cookie 直连也会被 WAF 拦截(返回 <meta name="aliyun_waf_aa">),
    必须让真实 Chrome 执行 JS 完成挑战,再在浏览器上下文内 fetch 才能访问 API。

    Args:
        base_url: 站点地址
        session_cookie: 可选 session cookie(注入到浏览器)
        auth_headers: 额外请求头(Authorization / New-Api-User 等)
        proxy: 代理
        max_wait: 最多等待秒数
        verbose: 详细输出
        path: 要请求的 API 路径
        method: GET/POST

    Returns:
        (status_code, response_dict) 或 (None, None) 失败
    """
    if proxy is None:
        proxy = _get_system_proxy()
    auth_headers = auth_headers or {}
    proc, dbg_port = _launch_chrome(proxy, url=base_url)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = None
            last_err = None
            for _ in range(3):
                try:
                    browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{dbg_port}')
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(3)
            if browser is None:
                raise RuntimeError(f'CDP 连接失败: {last_err}')
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 导航到站点(必须先导航到目标域,Cookie 才能写入)
            try:
                page.goto(base_url, wait_until='domcontentloaded', timeout=45000)
            except Exception:
                pass

            # 用 CDP Network.setCookie 注入 session(比 add_cookies 稳定)
            if session_cookie:
                try:
                    domain = base_url.replace('https://', '').replace('http://', '').split('/')[0]
                    cdp = ctx.new_cdp_session(page)
                    cdp.send('Network.setCookie', {
                        'name': 'session', 'value': session_cookie,
                        'domain': domain, 'path': '/',
                        'url': base_url + '/',
                    })
                    if verbose:
                        print('[SolveAPI] session cookie 注入成功(CDP)')
                except Exception as e:
                    # 回退:页面 evaluate 设置
                    try:
                        page.evaluate("(v) => { document.cookie = 'session=' + v + '; path=/'; }", session_cookie)
                        if verbose:
                            print('[SolveAPI] session cookie 注入成功(JS)')
                    except Exception as e2:
                        if verbose:
                            print(f'[SolveAPI] session cookie 注入失败: {e} / {e2}')

            # 定义目标 API 路径(供二次挑战导航使用)
            api_path = path if path.startswith('/') else '/' + path

            # 导航触发 WAF JS 挑战;等待 WAF cookie 就绪 + 页面真实内容
            try:
                page.goto(base_url, wait_until='domcontentloaded', timeout=45000)
            except Exception:
                pass
            t0 = time.time()
            waf_ready = False
            while time.time() - t0 < max_wait:
                # 检查 acw_tc 等 WAF cookie 是否已种下
                try:
                    cookies = ctx.cookies()
                    has_waf = any(c['name'] in ('acw_tc', 'cdn_sec_tc', 'acw_sc__v2', 'aliyungf_tc') for c in cookies)
                except Exception:
                    has_waf = False
                try:
                    body = page.evaluate("document.body ? document.body.innerText.slice(0,120) : ''")
                    title = page.title()
                    # 检测真实页面元素(登录表单 / 应用按钮),而非挑战页空壳
                    has_ui = page.evaluate("""() => {
                        const s = document.querySelectorAll('input, button, a, .semi-card, #app, #root').length;
                        const t = document.body ? document.body.innerText.trim().length : 0;
                        return s > 0 && t > 0;
                    }""")
                except Exception:
                    body, title, has_ui = '', '', False
                # 挑战页关键词(阿里云 WAF / CF / 网宿)
                is_challenge = any(k in body for k in ('验证', '安全', 'captcha', 'verify', 'Access denied',
                                                        '请稍候', 'Just a moment', '检查', 'aliyun'))
                if not is_challenge and has_ui:
                    waf_ready = True
                    if verbose:
                        print('[SolveAPI] WAF 已通过(页面出现真实内容)')
                    break
                if has_waf and not is_challenge:
                    # 已有 WAF cookie 且无挑战关键词,尝试放行
                    waf_ready = True
                    if verbose:
                        print('[SolveAPI] WAF cookie 已就绪,尝试继续')
                    break
                # 中途 reload 帮助 WAF JS 重新执行
                if time.time() - t0 > 8 and int(time.time() - t0) % 10 == 0:
                    try:
                        page.reload(wait_until='domcontentloaded', timeout=20000)
                    except Exception:
                        pass
                if verbose and int(time.time() - t0) % 6 == 0:
                    print(f'[SolveAPI] 等待 WAF: title={title[:30]!r} has_ui={has_ui} waf_cookie={has_waf}')
                time.sleep(3)

            # 关键:先导航到目标 API 路径,触发阿里云 WAF 对该路径的二次 JS 挑战
            # (首页通过不代表 API 路径放行;goto 让挑战 JS 执行并种下 acw_sc__v2 二次 cookie)
            if waf_ready:
                try:
                    page.goto(base_url + api_path, wait_until='domcontentloaded', timeout=45000)
                except Exception:
                    pass
                t1 = time.time()
                while time.time() - t1 < 30:
                    try:
                        body2 = page.evaluate("document.body ? document.body.innerText.slice(0,120) : ''")
                        cookies2 = ctx.cookies()
                        has_sec = any(c['name'] in ('acw_sc__v2', 'cdn_sec_tc') for c in cookies2)
                    except Exception:
                        body2, has_sec = '', False
                    if body2.strip().startswith('{'):
                        if verbose:
                            print('[SolveAPI] API 路径挑战已通过(返回 JSON)')
                        break
                    if has_sec and not any(k in body2 for k in ('验证', '安全', 'Just a moment', '检查')):
                        if verbose:
                            print('[SolveAPI] 二次 WAF cookie 已种下,继续')
                        break
                    if verbose and int(time.time() - t1) % 6 == 0:
                        print(f'[SolveAPI] 等待 API 路径挑战: {body2[:30]!r}')
                    time.sleep(2.5)

            # 页面内 fetch(浏览器已持有有效 WAF cookie + session;补全浏览器行为头绕过
            # 阿里云 WAF 对 API 请求的二次校验)
            api_path = path if path.startswith('/') else '/' + path
            # fetch 带指数退避:挑战 JS 种 cookie 需要时间,首次 403/HTML 后间隔重试
            js = f'''async (args) => {{
                const h = Object.assign({{
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Referer': location.origin + '/',
                    'Origin': location.origin,
                    'Sec-Fetch-Dest': 'empty',
                    'Sec-Fetch-Mode': 'cors',
                    'Sec-Fetch-Site': 'same-origin',
                }}, args.headers || {{}});
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                let last = {{ status: 0, data: null, raw: 'no-request' }};
                for (let attempt = 0; attempt < 5; attempt++) {{
                    if (attempt > 0) await sleep(2500 * attempt + 1500);
                    try {{
                        const resp = await fetch('{api_path}', {{
                            method: '{method}',
                            headers: h,
                            credentials: 'include',
                        }});
                        const text = await resp.text();
                        try {{ last = {{ status: resp.status, data: JSON.parse(text) }}; }}
                        catch (e) {{ last = {{ status: resp.status, data: null, raw: text.substring(0, 300) }}; }}
                        // 成功或非 WAF HTML 即停
                        const isWaf = (last.raw && (last.raw.includes('aliyun_waf') || last.raw.includes('Just a moment')
                            || last.raw.includes('安全验证') || last.raw.toLowerCase().includes('<!doctype')));
                        if (last.data !== null || !isWaf) return last;
                    }} catch (e) {{
                        last = {{ status: 0, data: null, raw: 'fetch-error: ' + e.message }};
                    }}
                }}
                return last;
            }}'''
            result = page.evaluate(js, {'headers': auth_headers})
            # 若返回的是 WAF HTML(非 JSON),reload 后等 WAF JS 跑完再试
            for _retry in range(4):
                if result.get('data') is not None and not (isinstance(result.get('data'), dict) and result['data'].get('message', '').startswith('<!')):
                    break
                raw = result.get('raw', '')
                dumped = str(result.get('data', ''))
                is_waf = ('aliyun_waf' in raw or 'Just a moment' in raw or '安全验证' in raw
                          or '<!doctype' in raw.lower() or '<html' in raw.lower() or 'aliyun_waf' in dumped)
                if not is_waf:
                    break
                if verbose:
                    print(f'[SolveAPI] 仍被 WAF 拦截(第 {_retry+1} 次),reload 后等待 WAF JS...')
                try:
                    page.reload(wait_until='domcontentloaded', timeout=20000)
                except Exception:
                    pass
                # 等 WAF JS 完成(检测 acw_sc__v2 等二次 cookie 或页面真实 UI)
                w2 = time.time()
                while time.time() - w2 < 25:
                    try:
                        cookies_now = ctx.cookies()
                        has_sec = any(c['name'] in ('acw_sc__v2', 'cdn_sec_tc') for c in cookies_now)
                        ui = page.evaluate("""() => document.querySelectorAll('input, button, a').length > 0 && (document.body ? document.body.innerText.trim().length > 0 : false)""")
                    except Exception:
                        has_sec, ui = False, False
                    if has_sec and ui:
                        break
                    time.sleep(2)
                result = page.evaluate(js, {'headers': auth_headers})
            if verbose:
                import json as _json
                if result.get('data') is not None:
                    print(f'[SolveAPI] {method} {path} -> HTTP {result.get("status")} '
                          f'data={_json.dumps(result.get("data"), ensure_ascii=False)[:120]}')
                else:
                    print(f'[SolveAPI] {method} {path} -> HTTP {result.get("status")} '
                          f'非JSON raw={result.get("raw", "")[:200]!r}')
            browser.close()
            return result.get('status'), result.get('data')
    except Exception as e:
        print(f'[SolveAPI] 执行失败: {e}')
        return None, None
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def solve_and_gwent(base_url: str, session_cookie: str = None, user_id: str = None,
                    access_token: str = None, proxy: str = None, max_wait: int = 120,
                    verbose: bool = True):
    """
    在真实 Chrome 会话内完成 CF 通过 + 维云翻卡(/api/gwent/draw)

    vsllm.cc 对 /api/gwent/* 与 /api/user/checkin 采用路径级 CF 挑战,
    requests/curl 直连必 403,playwright 直接启动的 Chrome 会因自动化特征被
    Turnstile 卡死;必须用真实 Chrome + CDP 连接 + 坐标点击勾选框(本仓库方案)。

    Args:
        base_url: 站点地址
        session_cookie: session cookie 值
        user_id: New-Api-User 头
        access_token: 可选 Authorization 令牌(优先级高于 session)
        proxy: 代理;默认读系统代理(GHA 无代理为 None)
        max_wait: 最大等待秒数

    Returns:
        dict:
          {"empty": true}                    已无翻卡次数
          {"success": true, "prize_name", "quota_awarded", "remaining"} 成功
          {"error": ...}                     失败
    """
    if proxy is None:
        proxy = _get_system_proxy()
    auth_headers = {}
    if access_token:
        auth_headers['Authorization'] = access_token
    if user_id:
        auth_headers['New-Api-User'] = str(user_id)

    proc, dbg_port = _launch_chrome(proxy, url=base_url)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = None
            last_err = None
            for _ in range(3):
                try:
                    browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{dbg_port}')
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(3)
            if browser is None:
                raise RuntimeError(f'CDP 连接失败: {last_err}')
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 注入 session cookie(有 user_id 时同时留 New-Api-User 头,但 cookie 更稳)
            if session_cookie:
                domain = base_url.replace('https://', '').replace('http://', '').split('/')[0]
                try:
                    ctx.add_cookies([{'name': 'session', 'value': session_cookie,
                                      'domain': domain, 'path': '/'}])
                except Exception as e:
                    if verbose:
                        print(f'[Turnstile] 注入 session cookie 失败: {e}')

            if verbose:
                print(f'[Turnstile] 导航并等待 CF 通过: {base_url}')
            if not _wait_cf_pass(page, base_url, timeout=60, verbose=verbose):
                if verbose:
                    print('[Turnstile] CF 验证未通过(首页)')
                return {'error': 'CF 首页挑战未通过'}

            # 翻卡目标路径:先导航触发该路径的 CF 挑战,等返回 JSON 即通过
            api_path = '/api/gwent/status'
            try:
                page.goto(base_url + api_path, wait_until='domcontentloaded', timeout=45000)
            except Exception:
                pass
            t0 = time.time()
            passed = False
            while time.time() - t0 < 60:
                try:
                    body = page.evaluate("document.body ? document.body.innerText.slice(0,150) : ''")
                    title = page.title()
                except Exception:
                    body, title = '', ''
                if body.strip().startswith('{'):
                    passed = True
                    if verbose:
                        print('[Turnstile] 翻卡路径挑战已通过')
                    break
                # 挑战页可能是中文"请稍候"/"正在进行安全验证"
                if verbose and int(time.time()) % 8 == 0:
                    print(f'[Turnstile] 等待路径挑战: title={title[:30]!r} body={body[:30]!r}')
                if time.time() - t0 > 10:
                    try:
                        page.reload(wait_until='domcontentloaded', timeout=20000)
                    except Exception:
                        pass
                    t0_reload = time.time()
                    # reload 后最多 20s 再看
                    while time.time() - t0_reload < 20:
                        try:
                            body = page.evaluate("document.body ? document.body.innerText.slice(0,150) : ''")
                        except Exception:
                            body = ''
                        if body.strip().startswith('{'):
                            passed = True
                            break
                        time.sleep(3)
                    if passed:
                        break
                else:
                    time.sleep(4)
            if not passed:
                if verbose:
                    print('[Turnstile] 翻卡路径挑战未通过,尝试直接翻卡')
            # 尝试 Turnstile 手动勾选(若挑战页内嵌组件)
            try:
                sitekey = page.evaluate('''async () => {
                    try {
                        const r = await fetch('/api/status');
                        const j = await r.json();
                        return (j.data && j.data.turnstile_site_key) || null;
                    } catch (e) { return null; }
                }''')
            except Exception:
                sitekey = None

            GWENT_JS = '''async (headers, sessionSet) => {
                const H = Object.assign({'Content-Type': 'application/json'}, headers);
                const post = async (path, method) => {
                    const resp = await fetch(path, {
                        method: method || 'POST',
                        headers: Object.assign({'Accept': 'application/json'}, headers),
                        credentials: 'include',
                    });
                    const text = await resp.text();
                    try { return {json: JSON.parse(text), status: resp.status}; }
                    catch (e) { return {notJson: true, status: resp.status, text: text.substring(0, 150)}; }
                };
                // 查询状态
                try {
                    const st = await post('/api/gwent/status', 'GET');
                    if (!st.notJson && st.json && st.json.success !== false) {
                        const d = st.json.data || {};
                        const charge = Number(d.charges_current || 0);
                        const extra = Number(d.extra_draws_left || 0);
                        if ((charge + extra) <= 0) {
                            return { empty: true, charges: charge, extra: extra, success: true, status: st.status };
                        }
                    }
                } catch (e) {}
                // 翻卡
                const res = await post('/api/gwent/draw', 'POST');
                if (res.notJson) {
                    if (res.text.includes('Just a moment') || res.text.includes('安全验证')) {
                        return { error: 'CF 拦截翻卡接口', httpStatus: res.status, success: false };
                    }
                    return { error: '响应非 JSON: ' + res.text, httpStatus: res.status, success: false };
                }
                const data = res.json;
                if (data.success !== true) {
                    return { success: false, message: data.message || data.msg || '翻卡失败', httpStatus: res.status };
                }
                const d = data.data || {};
                const prize = d.prize || {};
                // 翻卡后:分享解锁下次 +50% (share_unlock 激活 next_bonus_pct)
                let share_ok = false, share_msg = '';
                try {
                    const shr = await post('/api/gwent/share_unlock', 'POST');
                    share_ok = !!(shr.json && shr.json.success);
                    share_msg = shr.json ? (shr.json.message || shr.json.msg || '') : '';
                } catch (e) { share_msg = String(e); }
                return {
                    success: true,
                    prize_name: prize.name || '?',
                    quota_awarded: prize.quota || 0,
                    rarity: prize.rarity || '',
                    applied_bonus_pct: Number(d.applied_bonus_pct || 0),
                    should_prompt_share: !!d.should_prompt_share,
                    share_ok: share_ok,
                    share_msg: share_msg,
                    remaining: (Number(d.charges_current || 0) + Number(d.extra_draws_left || 0)),
                    status: res.status
                };
            }'''

            result = page.evaluate(GWENT_JS, auth_headers)
            if verbose:
                import json as _json
                print(f'[Turnstile] 翻卡结果: {_json.dumps(result, ensure_ascii=False)[:200]}')
            browser.close()
            return result if isinstance(result, dict) else {'error': str(result)}
    except Exception as e:
        print(f'[Turnstile] 翻卡求解失败: {e}')
        return {'error': str(e)}
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def solve_and_gwent_tasks(base_url: str, session_cookie: str = None, user_id: str = None,
                          access_token: str = None, proxy: str = None, max_wait: int = 120,
                          verbose: bool = True):
    """
    单会话完成维云"看广告"+"答题"两个日常任务(奖励=充能翻卡次数)。

    流程:
      1. 真实 Chrome + CDP 过 WAF / 路径级挑战
      2. GET /api/gwent/status 解析 tasks.task2(看广告) / task3(答题)
      3. 看广告: POST /api/gwent/ad/start -> 真等 duration_sec -> POST /api/gwent/ad/claim
         (后端时间戳硬墙,必须真实等待;受 min_interval_sec 与 daily_cap 限制)
      4. 答题: POST /api/gwent/task3/start 拿题目 {text,image_url,options};
         若 status 下发的 quizzes 含正确索引则自动答对,否则记录题目跳过(答案在后端不暴露)

    Returns:
        {"ad": {广告结果}, "quiz": {答题结果}, "log": [...]}
    """
    if proxy is None:
        proxy = _get_system_proxy()
    auth_headers = {}
    if access_token:
        auth_headers['Authorization'] = access_token
    if user_id:
        auth_headers['New-Api-User'] = str(user_id)

    proc, dbg_port = _launch_chrome(proxy, url=base_url)
    import json as _json
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = None
            for _ in range(3):
                try:
                    browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{dbg_port}')
                    break
                except Exception as e:
                    time.sleep(3)
            if browser is None:
                return {'error': 'CDP 连接失败'}
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            if session_cookie:
                domain = base_url.replace('https://', '').replace('http://', '').split('/')[0]
                try:
                    ctx.add_cookies([{'name': 'session', 'value': session_cookie,
                                      'domain': domain, 'path': '/'}])
                except Exception:
                    pass

            if not _wait_cf_pass(page, base_url, timeout=60, verbose=verbose):
                return {'error': 'CF 首页挑战未通过'}

            # 导航 gwent status 触发路径级挑战
            try:
                page.goto(base_url + '/api/gwent/status', wait_until='domcontentloaded', timeout=45000)
            except Exception:
                pass
            t0 = time.time()
            while time.time() - t0 < 60:
                try:
                    body = page.evaluate("document.body ? document.body.innerText.slice(0,150) : ''")
                except Exception:
                    body = ''
                if body.strip().startswith('{'):
                    break
                time.sleep(3)

            def fetch_api(path, method='GET'):
                js = (
                    "async (args) => {"
                    "  const h = Object.assign({"
                    "    'Accept':'application/json','X-Requested-With':'XMLHttpRequest',"
                    "    'Referer': location.origin + '/', 'Origin': location.origin,"
                    "    'Sec-Fetch-Dest':'empty','Sec-Fetch-Mode':'cors','Sec-Fetch-Site':'same-origin'"
                    "  }, args.headers || {});"
                    "  const resp = await fetch('" + path + "', { method:'" + method + "', headers:h, credentials:'include' });"
                    "  const text = await resp.text();"
                    "  try { return { status: resp.status, data: JSON.parse(text) }; }"
                    "  catch(e) { return { status: resp.status, data: null, raw: text.substring(0,200) }; }"
                    "}"
                )
                return page.evaluate(js, {'headers': auth_headers})

            result = {'ad': {}, 'quiz': {}, 'log': []}

            # 1) 拿 status 的 tasks
            st = fetch_api('/api/gwent/status', 'GET')
            tasks = {}
            if st and st.get('data'):
                d = st['data']
                if isinstance(d, dict):
                    inner = d.get('data') if isinstance(d.get('data'), dict) else {}
                    tasks = inner.get('tasks') if isinstance(inner, dict) else (tasks or {})
            ad = tasks.get('task2') if isinstance(tasks, dict) else {}
            quiz = tasks.get('task3') if isinstance(tasks, dict) else {}
            if verbose:
                print(f'[Tasks] ad.enabled={ad.get("enabled")} daily_cap={ad.get("daily_cap")} '
                      f'done={ad.get("done_count")} dur={ad.get("duration_sec")} '
                      f'next_at={ad.get("next_available_at")}')
                print(f'[Tasks] quiz.enabled={quiz.get("enabled")} suspended={quiz.get("suspended")} '
                      f'time_limit={quiz.get("time_limit_sec")} quizzes={len(quiz.get("quizzes") or [])}')
            result['tasks_raw'] = {'ad': ad, 'quiz': quiz}
            if verbose:
                print(f'[Tasks] raw task2={_json.dumps(ad, ensure_ascii=False)[:200]}')
                print(f'[Tasks] raw task3={_json.dumps(quiz, ensure_ascii=False)[:200]}')

            # 2) 看广告(判断依据:task2 存在 + 未暂停 + 剩余次数>0;无 enabled 字段)
            ad_left = int((ad or {}).get('daily_cap') or 0) - int((ad or {}).get('done_count') or 0)
            if ad and not ad.get('suspended') and ad_left > 0:
                left = int(ad.get('daily_cap') or 0) - int(ad.get('done_count') or 0)
                done_ad = []
                for i in range(max(0, left)):
                    # min_interval 间隔检查
                    nxt = ad.get('next_available_at')
                    if nxt and time.time() < int(nxt or 0):
                        if verbose:
                            print(f'[Tasks] 广告处于间隔冷却,跳过剩余(下次可用 '
                                  f'{datetime.fromtimestamp(int(nxt)).strftime("%H:%M:%S")})')
                        break
                    s2 = fetch_api('/api/gwent/ad/start', 'POST')
                    if not (s2 and s2.get('data') and s2['data'].get('success')):
                        msg = (s2.get('data') or {}).get('message') if s2 else '网络错误'
                        if verbose:
                            print(f'[Tasks] 广告开始失败: {msg}')
                        break
                    dur = int((s2['data'].get('data') or {}).get('duration_sec') or 15)
                    if verbose:
                        print(f'[Tasks] 看广告中,等待 {dur} 秒...')
                    time.sleep(dur + 1)  # 后端时间戳硬墙,真实等待
                    c2 = fetch_api('/api/gwent/ad/claim', 'POST')
                    ok = bool(c2 and c2.get('data') and c2['data'].get('success'))
                    done_ad.append({
                        'ok': ok,
                        'msg': (c2.get('data') or {}).get('message', '') if c2 else '网络错误',
                    })
                    if verbose:
                        print(f'[Tasks] 广告 #%d 领奖: %s' % (i + 1,
                              '成功' if ok else (done_ad[-1]['msg'])))
                    # 重新拉 status 更新剩余次/next_available_at
                    st3 = fetch_api('/api/gwent/status', 'GET')
                    if st3 and st3.get('data'):
                        d3 = st3['data']
                        tasks3 = {}
                        if isinstance(d3, dict):
                            inner3 = d3.get('data') if isinstance(d3.get('data'), dict) else {}
                            tasks3 = inner3.get('tasks') if isinstance(inner3, dict) else {}
                        ad = tasks3.get('task2') if isinstance(tasks3, dict) else {}
                result['ad'] = {'done': len([x for x in done_ad if x['ok']]),
                                'detail': done_ad}
            else:
                if verbose:
                    print('[Tasks] 看广告未开启或暂停,跳过')
                result['ad'] = {'done': 0, 'detail': [], 'note': '未开启'}

            # 3) 答题(判断依据:task3 存在 + 未暂停;无 enabled 字段)
            if quiz and not quiz.get('suspended'):
                s3 = fetch_api('/api/gwent/task3/start', 'POST')
                if s3 and s3.get('data') and s3['data'].get('success'):
                    qd = s3['data'].get('data') or {}
                    question = qd.get('question') or {}
                    qtext = question.get('text', '')
                    qoptions = question.get('options') or []
                    # 尝试从 status 下发的 quizzes 找正确索引
                    answer_idx = None
                    quizzes = quiz.get('quizzes') or []
                    for q in quizzes:
                        if q.get('text') == qtext and ('correct_index' in q):
                            answer_idx = int(q.get('correct_index', -1))
                            break
                    result['quiz']['question'] = qtext
                    result['quiz']['options'] = qoptions
                    # 无正确答案则随机选一个(答对概率比不答强)
                    if answer_idx is None or answer_idx < 0 or answer_idx >= len(qoptions):
                        import random as _random
                        answer_idx = _random.randint(0, max(0, len(qoptions) - 1)) if qoptions else _random.randint(0, 3)
                        result['quiz']['guessed'] = True
                    if answer_idx is not None:
                        a3 = fetch_api('/api/gwent/task3/answer', 'POST')
                        # answer 需要 body {answer_index}
                        # 用专门带 body 的 fetch
                        a3 = page.evaluate(
                            "async (args) => {"
                            "  const h = Object.assign({'Accept':'application/json','Content-Type':'application/json','X-Requested-With':'XMLHttpRequest','Referer': location.origin + '/','Origin': location.origin,'Sec-Fetch-Dest':'empty','Sec-Fetch-Mode':'cors','Sec-Fetch-Site':'same-origin'}, args.headers || {});"
                            "  const resp = await fetch('/api/gwent/task3/answer', { method:'POST', headers:h, body: JSON.stringify({answer_index: args.answer_index}), credentials:'include' });"
                            "  const text = await resp.text();"
                            "  try { return { status: resp.status, data: JSON.parse(text) }; }"
                            "  catch(e) { return { status: resp.status, data: null, raw: text.substring(0,200) }; }"
                            "}",
                            {'headers': auth_headers, 'answer_index': answer_idx})
                        if a3 and a3.get('data') and a3['data'].get('success'):
                            correct = (a3['data'].get('data') or {}).get('correct')
                            result['quiz']['answered'] = True
                            result['quiz']['correct'] = correct
                            if verbose:
                                print(f'[Tasks] 答题提交 index={answer_idx} -> correct={correct}')
                        else:
                            result['quiz']['answered'] = False
                            result['quiz']['msg'] = (a3.get('data') or {}).get('message', '') if a3 else '网络错误'
                            if verbose:
                                print(f'[Tasks] 答题提交失败: {result["quiz"]["msg"]}')
                    else:
                        result['quiz']['answered'] = False
                        result['quiz']['note'] = 'quizzes 未下发答案,需人工答题(题目已记录)'
                        if verbose:
                            print(f'[Tasks] 答题未自动答: 题目={qtext!r} 选项={qoptions}')
                else:
                    result['quiz']['answered'] = False
                    result['quiz']['msg'] = 'task3/start 失败'
                    if verbose:
                        print('[Tasks] 答题开始失败')
            else:
                if verbose:
                    print('[Tasks] 答题未开启或暂停,跳过')
                result['quiz'] = {'answered': False, 'note': '未开启'}

            browser.close()
            return result
    except Exception as e:
        print(f'[Tasks] 任务执行失败: {e}')
        return {'error': str(e)}
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
