
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from core import confirm
from core.desktop_control import DesktopError, get_service

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
)
_OS = platform.system()   # "Windows" | "Darwin" | "Linux"
_navigation_callback = None


def bind_navigation(callback):
    """Bind an opt-in observer; browser callbacks never capture screenshots."""
    global _navigation_callback
    _navigation_callback = callback


def _safe_error(error):
    return str(error) if isinstance(error, DesktopError) else type(error).__name__


def _cdp_endpoint():
    endpoint = os.environ.get("CHIDVI_CHROME_CDP", "").strip()
    if not endpoint:
        return ""
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in ("http", "https", "ws", "wss") or
            parsed.hostname not in ("localhost", "127.0.0.1", "::1") or
            parsed.username or parsed.password or not parsed.port):
        raise ValueError("CHIDVI_CHROME_CDP must be an explicit loopback CDP URL with a port.")
    return endpoint


def _safe_url(value):
    parsed = urlsplit(value)
    # Paths as well as queries/fragments can contain reset/authentication tokens.
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if parsed.port:
        hostname += f":{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, "", "", ""))


_INSPECT_PAGE = r"""() => {
 const visible = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e); return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'; };
 const secret = e => /password|one[ -]?time|\botp\b|token|secret|verification|passcode|security code|auth.*code|\bpin\b|credit.card|cc-number|cc-csc|cc-exp/i.test([e.type,e.name,e.id,e.autocomplete,e.getAttribute('aria-label'),e.getAttribute('placeholder'),[...(e.labels||[])].map(l=>l.textContent).join(' ')].join(' '));
 const challenge = /captcha|verify (you are|you're)|human verification|security check/i.test(document.title) ||
   [...document.querySelectorAll('iframe')].some(e => /captcha|challenge/i.test((e.title||'')+' '+(e.src||'')) && visible(e));
 const sensitive = [...document.querySelectorAll('input,textarea,[contenteditable=true]')].some(e=>secret(e)&&visible(e));
 const controls = [...document.querySelectorAll('button,a[href],input,textarea,select,[role],[contenteditable=true]')].filter(visible).slice(0,200).map(e=>{
   const r=e.getBoundingClientRect(), blocked=secret(e), role=e.getAttribute('role')||({BUTTON:'button',A:'link',SELECT:'combobox',TEXTAREA:'textbox',INPUT:e.type==='checkbox'?'checkbox':e.type==='radio'?'radio':'textbox'}[e.tagName]||'control');
   const refs = id => (id||'').split(/\s+/).map(i=>document.getElementById(i)?.textContent||'').join(' ').trim();
   const ownText = ['button','link','menuitem','tab','checkbox','radio','option'].includes(role)&&e.tagName!=='INPUT'?e.innerText:'';
   const name=blocked?'[credential control]':(e.getAttribute('aria-label')||refs(e.getAttribute('aria-labelledby'))||[...(e.labels||[])].map(l=>l.textContent).join(' ')||e.placeholder||ownText||e.title||e.alt||'');
   return {role,name:name.trim().slice(0,160),description:blocked?'':refs(e.getAttribute('aria-describedby')).slice(0,160),placeholder:blocked?'':(e.getAttribute('placeholder')||'').slice(0,120),
     enabled:!e.disabled&&e.getAttribute('aria-disabled')!=='true',selected:e.getAttribute('aria-selected')==='true',checked:!!e.checked,focused:e===document.activeElement,expanded:e.getAttribute('aria-expanded'),
     sensitive:blocked,bounds:{x:r.x,y:r.y,width:r.width,height:r.height}};
 });
 return {title:sensitive?'[sensitive page]':location.hostname,challenge,sensitive,controls,
   viewport:{width:innerWidth,height:innerHeight,device_pixel_ratio:devicePixelRatio}};
}"""

_TARGET_STATE = r"""e => ({tag:e.tagName,role:e.getAttribute('role'),name:(e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.innerText||'').slice(0,160),
 secret:/password|one[ -]?time|\botp\b|token|secret|verification|passcode|security code|auth.*code|\bpin\b|credit.card|cc-number|cc-csc|cc-exp/i.test([e.type,e.name,e.id,e.autocomplete,e.getAttribute('aria-label'),e.getAttribute('placeholder'),[...(e.labels||[])].map(l=>l.textContent).join(' ')].join(' ')),
 href:(e.tagName==='A'?new URL(e.href).origin:''),type:e.type||'',disabled:!!e.disabled})"""

_CLICK_RING = r"""async box => {
 const old=document.getElementById('__chidvi_click_ring'); if(old)old.remove();
 const e=document.createElement('div');e.id='__chidvi_click_ring';e.setAttribute('aria-hidden','true');
 Object.assign(e.style,{position:'fixed',left:(box.x+box.width/2-22)+'px',top:(box.y+box.height/2-22)+'px',width:'44px',height:'44px',border:'4px solid #ff3b30',borderRadius:'50%',pointerEvents:'none',zIndex:'2147483647',boxSizing:'border-box',background:'transparent'});
 document.documentElement.appendChild(e);
 if(!matchMedia('(prefers-reduced-motion: reduce)').matches)e.animate([{transform:'scale(.8)',opacity:1},{transform:'scale(1.5)',opacity:0.2}],{duration:800,iterations:2});
 setTimeout(()=>e.remove(),1600); await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))); return true;
}"""

def _normalize_url(url: str) -> str:
    """
    Bare words like "instagram" → "https://instagram.com"
    Domains like "instagram.com" → "https://instagram.com"
    Full URLs pass through unchanged.
    """
    url = url.strip()
    if not url:
        return "about:blank"
    if url == "about:blank":
        return url
    if "://" in url:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise DesktopError("Use an HTTP(S) website without embedded credentials.")
        return url
    # No dot at all → assume .com  (e.g. "instagram" → "instagram.com")
    if "." not in url:
        url = url + ".com"
    return "https://" + url


def _user_agent() -> str:
    if _OS == "Windows":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/5567.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/5567.36"
        )
    if _OS == "Darwin":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/5567.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/5567.36"
        )
    return (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/5567.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/5567.36"
    )


def _real_profile_dir(browser: str) -> str:
    home  = Path.home()
    local = os.environ.get("LOCALAPPDATA", "")
    roam  = os.environ.get("APPDATA", "")

    candidates: list[Path] = []

    if _OS == "Windows":
        m = {
            "chrome":   [Path(local) / "Google"          / "Chrome"          / "User Data"],
            "edge":     [Path(local) / "Microsoft"        / "Edge"            / "User Data"],
            "brave":    [Path(local) / "BraveSoftware"    / "Brave-Browser"   / "User Data"],
            "vivaldi":  [Path(local) / "Vivaldi"          / "User Data"],
            "opera":    [Path(roam)  / "Opera Software"   / "Opera Stable",
                         Path(local) / "Opera Software"   / "Opera Stable"],
            "operagx":  [Path(roam)  / "Opera Software"   / "Opera GX Stable",
                         Path(local) / "Opera Software"   / "Opera GX Stable"],
        }
        candidates = m.get(browser, [])

    elif _OS == "Darwin":
        lib = home / "Library" / "Application Support"
        m = {
            "chrome":   [lib / "Google"             / "Chrome"],
            "edge":     [lib / "Microsoft Edge"],
            "brave":    [lib / "BraveSoftware"       / "Brave-Browser"],
            "vivaldi":  [lib / "Vivaldi"],
            "opera":    [lib / "com.operasoftware.Opera"],
            "operagx":  [lib / "com.operasoftware.OperaGX"],
        }
        candidates = m.get(browser, [])

    elif _OS == "Linux":
        cfg = home / ".config"
        m = {
            "chrome":   [cfg / "google-chrome", cfg / "chromium"],
            "edge":     [cfg / "microsoft-edge"],
            "brave":    [cfg / "BraveSoftware" / "Brave-Browser"],
            "vivaldi":  [cfg / "vivaldi"],
            "opera":    [cfg / "opera"],
            "operagx":  [cfg / "opera-gx"],
        }
        candidates = m.get(browser, [])

    for p in candidates:
        if p.exists():
            print(f"[Browser] ✅ Real profile found for {browser}: {p}")
            return str(p)

    fallback = home / ".jarvis_profiles" / browser
    fallback.mkdir(parents=True, exist_ok=True)
    print(f"[Browser] ⚠️  Real profile not found for {browser}, using: {fallback}")
    return str(fallback)

def _firefox_profile_dir() -> Optional[str]:
    home = Path.home()

    if _OS == "Windows":
        base = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox"
    elif _OS == "Darwin":
        base = home / "Library" / "Application Support" / "Firefox"
    else:
        base = home / ".mozilla" / "firefox"

    ini = base / "profiles.ini"
    if not ini.exists():
        return None

    current: dict[str, str] = {}
    default_path: Optional[str] = None

    for line in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("["):
            p = current.get("Path", "")
            if p and current.get("Default") == "1":
                is_rel = current.get("IsRelative", "1") == "1"
                default_path = str(base / p) if is_rel else p
            current = {}
        elif "=" in line:
            k, _, v = line.partition("=")
            current[k.strip()] = v.strip()

    p = current.get("Path", "")
    if p and current.get("Default") == "1":
        is_rel = current.get("IsRelative", "1") == "1"
        default_path = str(base / p) if is_rel else p

    if default_path and Path(default_path).exists():
        print(f"[Browser] Firefox real profile: {default_path}")
        return default_path
    return None

def _find_opera_windows() -> Optional[str]:
    local  = os.environ.get("LOCALAPPDATA", "")
    prog   = os.environ.get("PROGRAMFILES", "")
    prog86 = os.environ.get("PROGRAMFILES(X86)", "")

    candidates = [
        Path(local)  / "Programs" / "Opera"    / "opera.exe",
        Path(local)  / "Programs" / "Opera GX" / "opera.exe",
        Path(prog)   / "Opera"    / "opera.exe",
        Path(prog86) / "Opera"    / "opera.exe",
    ]
    for p in candidates:
        if p.exists():
            print(f"[Browser] Opera found at: {p}")
            return str(p)

    try:
        import winreg
        keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe",
            r"SOFTWARE\Clients\StartMenuInternet\OperaStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\OperaGXStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\opera\shell\open\command",
        ]
        for key_path in keys:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        print(f"[Browser] Opera found via registry: {exe}")
                        return exe
                except Exception:
                    continue
    except Exception:
        pass

    return shutil.which("opera") or None

def _find_exe_windows(prog_name: str) -> Optional[str]:
    try:
        import winreg
        paths_to_try = [
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{prog_name}.exe",
            rf"SOFTWARE\Clients\StartMenuInternet\{prog_name}\shell\open\command",
        ]
        for key_path in paths_to_try:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        return exe
                except Exception:
                    continue
    except Exception:
        pass
    return None

_BROWSER_SPECS: dict[str, dict] = {
    "Windows": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": []},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox.exe"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera.exe"],  "special": "opera_windows"},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": [],             "special": "opera_windows"},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave.exe"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi.exe"]},
        "safari":   None,
    },
    "Darwin": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": ["microsoft-edge"]},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi"]},
        "safari":   {"engine": "webkit",   "channel": None,      "bins": []},
    },
    "Linux": {
        "chrome":   {"engine": "chromium", "channel": None,
                     "bins": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]},
        "edge":     {"engine": "chromium", "channel": None,
                     "bins": ["microsoft-edge", "microsoft-edge-stable"]},
        "firefox":  {"engine": "firefox",  "channel": None, "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "operagx":  {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "brave":    {"engine": "chromium", "channel": None, "bins": ["brave-browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None, "bins": ["vivaldi-stable", "vivaldi"]},
        "safari":   None,
    },
}

_ALIASES: dict[str, str] = {
    "google chrome":   "chrome",
    "google-chrome":   "chrome",
    "microsoft edge":  "edge",
    "ms edge":         "edge",
    "msedge":          "edge",
    "mozilla firefox": "firefox",
    "opera gx":        "operagx",
    "opera_gx":        "operagx",
}


def _resolve_browser(name: str) -> dict | None:
    name   = _ALIASES.get(name.lower().strip(), name.lower().strip())
    os_map = _BROWSER_SPECS.get(_OS, {})
    spec   = os_map.get(name)
    if spec is None:
        return None

    engine  = spec["engine"]
    channel = spec.get("channel")
    bins    = spec.get("bins", [])
    exe     = None

    if spec.get("special") == "opera_windows":
        exe = _find_opera_windows()
        if not exe:
            print(f"[Browser] ⚠️  Opera executable not found on Windows.")
        return {"engine": engine, "exe": exe, "channel": channel}

    for b in bins:
        found = shutil.which(b)
        if found:
            exe = found
            break

    if not exe and _OS == "Darwin":
        app_names = {
            "chrome":  ["Google Chrome.app"],
            "edge":    ["Microsoft Edge.app"],
            "firefox": ["Firefox.app"],
            "opera":   ["Opera.app", "Opera GX.app"],
            "brave":   ["Brave Browser.app"],
            "vivaldi": ["Vivaldi.app"],
        }
        for app in app_names.get(name, []):
            app_dir = Path("/Applications") / app / "Contents" / "MacOS"
            if app_dir.exists():
                found_bins = list(app_dir.iterdir())
                if found_bins:
                    exe = str(found_bins[0])
                    break

    if not exe and _OS == "Windows" and not channel:
        exe = _find_exe_windows(name)

    return {"engine": engine, "exe": exe, "channel": channel}


def _detect_default_browser() -> str:
    try:
        if _OS == "Windows":
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\http\UserChoice",
            )
            prog_id = winreg.QueryValueEx(k, "ProgId")[0].lower()
            winreg.CloseKey(k)
            for kw in ("edge", "firefox", "opera", "brave", "vivaldi", "chrome"):
                if kw in prog_id:
                    return kw
        elif _OS == "Darwin":
            out = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "safari", "chrome", "edge"):
                if kw in out:
                    return kw
        elif _OS == "Linux":
            out = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "chrome", "edge"):
                if kw in out:
                    return kw
    except Exception:
        pass
    return "chrome"


_SEARCH_ENGINES: dict[str, str] = {
    "google":     "https://www.google.com/search?q=",
    "bing":       "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "yandex":     "https://yandex.com/search/?text=",
}

_MAC_APP_NAMES: dict[str, str] = {
    "chrome":  "Google Chrome",
    "edge":    "Microsoft Edge",
    "firefox": "Firefox",
    "opera":   "Opera",
    "operagx": "Opera GX",
    "brave":   "Brave Browser",
    "vivaldi": "Vivaldi",
    "safari":  "Safari",
}

# Windows registry lookup names for browsers whose spec has no explicit binary
_WIN_EXE_HINTS: dict[str, str] = {"chrome": "chrome", "edge": "msedge"}


def _open_native(url: str, browser_name: Optional[str]) -> str:
    """
    Opens the user's REAL browser normally — with their own profile,
    logged-in accounts and extensions. No automation attaches, so an
    about:blank tab or a blank profile NEVER shows up.
    If url is empty the browser starts with no URL (its own start page /
    session restore) — exactly as if the user had opened it themselves.
    Works on all three of Windows / macOS / Linux.
    """
    url = _normalize_url(url) if url and url.strip() else ""
    if url == "about:blank":
        url = ""

    name = None
    if browser_name:
        name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
    elif not url:
        # No URL → only a window will open; needs the default browser's exe
        name = _detect_default_browser()

    # Specific browser → launch its own executable, exactly like the user would.
    if name:
        if _OS == "Darwin":
            app = _MAC_APP_NAMES.get(name)
            if app:
                cmd = ["open", "-a", app] + ([url] if url else [])
                try:
                    subprocess.run(cmd, check=True, timeout=10)
                    return f"Opened in {name}: {_safe_url(url)}" if url else f"Opened {name}."
                except Exception as e:
                    print(f"[Browser] 'open -a {app}' failed ({_safe_error(e)}), trying binary…")

        spec = _resolve_browser(name)
        exe  = spec.get("exe") if spec else None
        if not exe and _OS == "Windows":
            if name in ("opera", "operagx"):
                exe = _find_opera_windows()
            else:
                exe = _find_exe_windows(_WIN_EXE_HINTS.get(name, name))
        if exe:
            try:
                subprocess.Popen(
                    [exe, url] if url else [exe],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return f"Opened in {name}: {_safe_url(url)}" if url else f"Opened {name}."
            except Exception as e:
                print(f"[Browser] Native launch failed for {name}: {_safe_error(e)}")
        print(f"[Browser] '{name}' not found — falling back to default browser.")

    if not url:
        return "Could not find a browser to open."

    # Default browser via the OS — exactly like the user clicking a link.
    try:
        if _OS == "Windows":
            os.startfile(url)                       # ShellExecute → default browser
        elif _OS == "Darwin":
            subprocess.run(["open", url], check=True, timeout=10)
        else:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return f"Opened in your default browser: {_safe_url(url)}"
    except Exception:
        try:
            if webbrowser.open(url):
                return f"Opened in your default browser: {_safe_url(url)}"
        except Exception:
            pass
        return f"Could not open a browser for: {_safe_url(url)}"


class _BrowserSession:
    """
    A full session for one browser instance.
    An explicit loopback CDP attachment or a separate automation profile.
    """

    def __init__(self, browser_name: str):
        self.browser_name = browser_name
        self._spec        = _resolve_browser(browser_name)

        self._loop:    asyncio.AbstractEventLoop | None = None
        self._thread:  threading.Thread | None          = None
        self._ready    = threading.Event()

        self._pw:      Playwright     | None = None
        self._context: BrowserContext | None = None
        self._page:    Page           | None = None
        self._attached = False
        self._browser = None
        self._init_error = None
        self._watched_context = None
        self._watched_pages = set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"BrowserThread-{self.browser_name}",
        )
        self._thread.start()
        self._ready.wait(timeout=20)
        if self._init_error:
            raise RuntimeError(f"Browser startup failed: {self._init_error}")
        if not self._ready.is_set():
            raise RuntimeError("Browser startup timed out.")

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_init())
        except Exception as exc:
            self._init_error = exc
            return
        finally:
            self._ready.set()
        self._loop.run_forever()

    async def _async_init(self):
        self._pw = await async_playwright().start()

    def run(self, coro, timeout: int = 60) -> str:
        if not self._loop:
            raise RuntimeError(f"Session for '{self.browser_name}' not started.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        deadline = time.monotonic() + timeout
        try:
            while True:
                get_service().check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise concurrent.futures.TimeoutError()
                try:
                    return future.result(timeout=min(0.1, remaining))
                except concurrent.futures.TimeoutError:
                    if future.done():
                        raise
        except (concurrent.futures.TimeoutError, DesktopError):
            future.cancel()
            raise

    def close(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._async_close(), self._loop).result(10)

    async def _async_close(self):
        if self._context and not self._attached:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = self._page = None
        self._browser = None
        self._attached = False
        self._watched_context = None
        self._watched_pages.clear()

    def _notify_navigation(self, page):
        asyncio.create_task(self._publish_navigation(page))

    async def _publish_navigation(self, page):
        if _navigation_callback:
            try:
                if not await page.evaluate("() => document.visibilityState === 'visible'"):
                    return
                url = _safe_url(page.url)
                sensitive = re.search(r"auth|password|sign.?in|log.?in|token|checkout|payment|verify|\b[A-Za-z0-9_-]{32,}\b", page.url, re.I)
                _navigation_callback(url, "Authentication" if sensitive else urlsplit(url).hostname or "Browser")
            except Exception:
                pass

    def _watch_page(self, page):
        if id(page) in self._watched_pages:
            return
        self._watched_pages.add(id(page))
        page.on("framenavigated", lambda frame: self._notify_navigation(page) if frame == page.main_frame else None)
        page.on("domcontentloaded", lambda: self._notify_navigation(page))

    async def _adopt_page(self) -> Page:
        """
        launch_persistent_context already opens a starting tab.
        Instead of opening a new blank tab (about:blank), it adopts that tab —
        so the user never sees an extra blank tab.
        """
        await asyncio.sleep(0.3)
        if self._watched_context is not self._context:
            await self._context.expose_binding("__chidvi_navigation", lambda source: self._notify_navigation(source["page"]))
            await self._context.add_init_script("""(() => { const notify=()=>{if(document.visibilityState==='visible')window.__chidvi_navigation().catch(()=>{});}; window.addEventListener('focus',notify); document.addEventListener('visibilitychange',notify); })();""")
            self._context.on("page", self._watch_page)
            self._watched_context = self._context
        pages = self._context.pages
        for page in pages:
            self._watch_page(page)
            await page.evaluate("""() => { if(window.__chidvi_navigation_installed)return;window.__chidvi_navigation_installed=true;const notify=()=>{if(document.visibilityState==='visible')window.__chidvi_navigation().catch(()=>{});};window.addEventListener('focus',notify);document.addEventListener('visibilitychange',notify); }""")
        if not pages:
            return await self._context.new_page()
        if len(pages) == 1:
            return pages[0]
        for page in pages:
            if await page.evaluate("() => document.hasFocus() && document.visibilityState === 'visible'"):
                return page
        raise DesktopError("Multiple browser tabs are open. Bring the intended tab to the foreground, then inspect again.")

    async def _launch(self):
        """
        Attaches to explicitly configured CDP or launches a dedicated profile.
        Does nothing if the context is already open.
        """
        if self._context is not None:
            return

        endpoint = _cdp_endpoint()
        if endpoint:
            self._browser = await self._pw.chromium.connect_over_cdp(endpoint, timeout=10_000)
            if not self._browser.contexts:
                raise RuntimeError("CDP browser has no accessible context.")
            self._context = self._browser.contexts[0]
            self._attached = True
            self._page = await self._adopt_page()
            return

        if self._spec is None:
            raise RuntimeError(
                f"'{self.browser_name}' bu platformda ({_OS}) desteklenmiyor."
            )

        engine_name = self._spec["engine"]
        exe         = self._spec["exe"]
        channel     = self._spec["channel"]
        engine_obj  = getattr(self._pw, engine_name)

        if engine_name == "firefox":
            profile = str(Path.home() / ".jarvis_profiles" / "firefox")
            kwargs: dict = {
                "headless":    False,
                "slow_mo":     0,
                "viewport":    None,
                "no_viewport": True,
                "timeout":     25_000,
            }
            if exe:
                kwargs["executable_path"] = exe
            try:
                self._context = await engine_obj.launch_persistent_context(profile, **kwargs)
            except Exception as e:
                print(f"[Browser] Firefox real profile failed ({_safe_error(e)}), using JARVIS profile")
                jarvis = str(Path.home() / ".jarvis_profiles" / "firefox_jarvis")
                Path(jarvis).mkdir(parents=True, exist_ok=True)
                self._context = await engine_obj.launch_persistent_context(jarvis, **kwargs)

            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Firefox launched")
            return

        if engine_name == "webkit":
            safari_profile = str(Path.home() / ".jarvis_profiles" / "safari")
            Path(safari_profile).mkdir(parents=True, exist_ok=True)
            kwargs = {
                "headless":    False,
                "slow_mo":     0,
                "viewport":    None,
                "no_viewport": True,
                "timeout":     25_000,
            }
            self._context = await engine_obj.launch_persistent_context(safari_profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Safari launched")
            return

        profile = str(Path.home() / ".jarvis_profiles" / self.browser_name)

        kwargs = {
            "headless":    False,
            "slow_mo":     0,
            "viewport":    None,
            "no_viewport": True,
            "timeout":     25_000,
            "args": [
                "--start-maximized",
                "--no-first-run",
                "--disable-default-apps",
                "--no-default-browser-check",
            ],
        }

        if exe:
            kwargs["executable_path"] = exe
        elif channel:
            kwargs["channel"] = channel

        label = (
            f"{self.browser_name}"
            + (f"/{channel}" if channel else "")
            + (f" @ {exe}" if exe else "")
        )

        try:
            self._context = await engine_obj.launch_persistent_context(profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Launched [{label}] profile={profile}")
            return
        except Exception as e:
            print(f"[Browser] ⚠️  Real profile failed for {label}: {_safe_error(e)}")

        # The real profile could not be opened (browser already open / locked
        # profile / newer Chrome versions block the real profile under
        # automation). Fall back to a persistent JARVIS automation profile —
        # accounts logged in here once stay logged in on later sessions too.
        jarvis_profile = str(Path.home() / ".jarvis_profiles" / self.browser_name)
        Path(jarvis_profile).mkdir(parents=True, exist_ok=True)
        print(f"[Browser] Retrying with JARVIS profile: {jarvis_profile}")

        try:
            self._context = await engine_obj.launch_persistent_context(jarvis_profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Launched [{label}] with JARVIS profile "
                  f"(sign-ins persist across sessions)")
        except Exception as e2:
            raise RuntimeError(f"Could not launch {self.browser_name}: {_safe_error(e2)}") from e2


    async def _get_page(self) -> Page:
        get_service().check_cancelled()
        await self._launch()
        focused = False
        for candidate in self._context.pages:
            if not candidate.is_closed():
                try:
                    if await candidate.evaluate("() => document.hasFocus() && document.visibilityState === 'visible'"):
                        if self._page is not candidate:
                            self._notify_navigation(candidate)
                        self._page = candidate
                        focused = True
                        break
                except Exception:
                    continue
        if self._attached and len(self._context.pages) > 1 and not focused:
            raise DesktopError("Bring the intended Chrome tab to the foreground before controlling it.")
        # If somehow page got closed, open a fresh one
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
            await asyncio.sleep(0.2)
        return self._page

    async def inspect(self, page=None):
        page = page or await self._get_page()
        state = await page.evaluate(_INSPECT_PAGE)
        state["url"] = _safe_url(page.url)
        state["connection"] = "attached CDP browser" if self._attached else "separate CHIDVI automation profile"
        return state

    async def _safe_page(self, page):
        get_service().check_cancelled()
        state = await self.inspect(page)
        if state.get("challenge"):
            raise DesktopError("Human verification challenge detected. Complete it manually before continuing.")
        if state.get("sensitive"):
            raise DesktopError("Credential/payment controls are visible. Complete the sensitive step manually.")
        return state

    async def _unique(self, locator):
        count = await locator.count()
        if count != 1:
            raise DesktopError(f"Expected one control; found {count}. Ask the user to clarify the target.")
        if not await locator.is_visible() or not await locator.is_enabled():
            raise DesktopError("Target is hidden or disabled.")
        return locator

    async def _click_target(self, page, locator, approved=False, expected=None):
        before = await self._safe_page(page)
        original_url = page.url
        locator = await self._unique(locator)
        target = await locator.evaluate(_TARGET_STATE)
        if target.get("secret"):
            raise DesktopError("Credential controls require manual input.")
        if expected is not None and (expected["target"] != target or expected["url"] != page.url):
            raise DesktopError("Target changed while awaiting confirmation; inspect again.")
        sensitive = target.get("type") == "submit" or bool(re.search(r"\b(pay|buy|purchase|checkout|delete|publish|send|submit|post|transfer|security|password)\b", target.get("name", ""), re.I))
        if sensitive and not approved:
            if confirm.pending_title():
                return "A desktop confirmation is already pending."
            return confirm.request("browser_click", "Confirm browser action", f"Click {target.get('name') or 'the selected control'} on {before['url']}",
                lambda: self.run(self._click_target(page, locator, approved=True, expected={"target": target, "url": original_url})))
        await locator.scroll_into_view_if_needed(timeout=5000)
        await page.bring_to_front()
        await asyncio.to_thread(get_service().speak_click)
        await self._safe_page(page)
        if page.url != original_url:
            raise DesktopError("Page navigated during speech; inspect again.")
        await self._unique(locator)
        if await locator.evaluate(_TARGET_STATE) != target:
            raise DesktopError("Target changed during speech; inspect again.")
        box = await locator.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise DesktopError("Target bounds are unavailable.")
        if await page.evaluate(_CLICK_RING, box) is not True:
            raise DesktopError("Click refused: page highlight unavailable.")
        get_service().check_cancelled()
        await self._safe_page(page)
        if page.url != original_url or await locator.evaluate(_TARGET_STATE) != target:
            raise DesktopError("Page or target changed during highlighting; inspect again.")
        if await locator.bounding_box() != box:
            raise DesktopError("Control moved during highlighting; inspect again.")
        get_service().check_cancelled()
        await locator.click(timeout=5000)
        # No retry after dispatch: a timeout or unchanged page is an uncertain outcome.
        try:
            after = await self.inspect(page)
            changed = before != after
            return json.dumps({"ok": True, "dispatched": True, "state_changed": changed,
                               "verified": False, "result": "Inspect the resulting state to verify the intended outcome.", "page": after})
        except Exception:
            return json.dumps({"ok": True, "dispatched": True, "verified": False,
                               "result": "Click dispatched; page became unavailable. Do not repeat automatically."})

    async def go_to(self, url: str) -> str:
        url = _normalize_url(url)
        page = await self._get_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            return f"Opened: {_safe_url(page.url)}"
        except Exception as exc:
            return f"Navigation failed or remains unverified ({_safe_error(exc)}). Inspect the page before continuing."

    async def search(self, query: str, engine: str = "google") -> str:
        base = _SEARCH_ENGINES.get(engine.lower(), _SEARCH_ENGINES["google"])
        return await self.go_to(base + query.replace(" ", "+"))

    async def click(self, selector: str = None, text: str = None) -> str:
        page = await self._get_page()
        try:
            if text:
                return await self._click_target(page, page.get_by_text(text, exact=True))
            if selector:
                return await self._click_target(page, page.locator(selector))
            return "No selector or text provided."
        except PlaywrightTimeout:
            return "Click timed out; outcome uncertain. Re-observe, do not repeat automatically."
        except Exception as e:
            return f"Click error: {_safe_error(e)}"

    async def type_text(self, selector: str = None, text: str = "",
                        clear_first: bool = True) -> str:
        page = await self._get_page()
        try:
            await self._safe_page(page)
            el = await self._unique(page.locator(selector) if selector else page.locator(":focus"))
            if (await el.evaluate(_TARGET_STATE)).get("secret"):
                raise DesktopError("Credential input must be handled manually.")
            if not isinstance(text, str) or len(text) > 20_000:
                raise DesktopError("Text must be a string of at most 20,000 characters.")
            if clear_first:
                await el.clear()
            await el.type(text, delay=50)
            return "Text typed."
        except Exception as e:
            return f"Type error: {_safe_error(e)}"

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self._get_page()
        try:
            y = amount if direction == "down" else -amount
            await page.mouse.wheel(0, y)
            return f"Scrolled {direction}."
        except Exception as e:
            return f"Scroll error: {_safe_error(e)}"

    async def press(self, key: str) -> str:
        page = await self._get_page()
        try:
            await self._safe_page(page)
            if key.lower().split("+")[-1] in ("enter", "return", "space", "numpadenter", " "):
                return "Use a named semantic click for activation/submission so it can be confirmed and highlighted."
            await page.keyboard.press(key)
            return f"Pressed: {key}"
        except Exception as e:
            return f"Key error: {_safe_error(e)}"

    async def get_text(self) -> str:
        page = await self._get_page()
        try:
            return json.dumps(await self.inspect(page))
        except Exception as e:
            return f"Could not get page text: {_safe_error(e)}"

    async def get_url(self) -> str:
        page = await self._get_page()
        return _safe_url(page.url)

    async def fill_form(self, fields: dict) -> str:
        page    = await self._get_page()
        await self._safe_page(page)
        if not isinstance(fields, dict) or len(fields) > 30:
            return "Provide at most 30 named form fields."
        results = []
        for selector, value in fields.items():
            try:
                result = await self.type_text(selector, str(value))
                results.append(result)
                if result != "Text typed.":
                    break
            except Exception as e:
                results.append(f"✗ {selector}: {_safe_error(e)}")
        return "Form filled: " + ", ".join(results)

    async def smart_click(self, description: str) -> str:
        page = await self._get_page()
        await self._safe_page(page)
        matches = []
        for role in ("button", "link", "searchbox", "textbox", "menuitem", "tab"):
            loc = page.get_by_role(role, name=description, exact=True)
            count = await loc.count()
            if count > 1:
                raise DesktopError("Multiple matching controls; ask the user to clarify.")
            if count:
                matches.append(loc)
        if len(matches) > 1:
            raise DesktopError("Multiple matching controls; ask the user to clarify.")
        if matches:
            return await self._click_target(page, matches[0])
        return await self._click_target(page, page.get_by_text(description, exact=True))

    async def smart_type(self, description: str, text: str) -> str:
        page = await self._get_page()
        await self._safe_page(page)
        candidates = [
            ("placeholder", page.get_by_placeholder(description, exact=True)),
            ("label",       page.get_by_label(description, exact=True)),
            ("role",        page.get_by_role("textbox", name=description, exact=True)),
            ("searchbox",   page.get_by_role("searchbox", name=description, exact=True)),
            ("combobox",    page.get_by_role("combobox", name=description, exact=True)),
        ]
        for method, loc in candidates:
            if not await loc.count():
                continue
            el = await self._unique(loc)
            if (await el.evaluate(_TARGET_STATE)).get("secret"):
                raise DesktopError("Credential input must be handled manually.")
            if not isinstance(text, str) or len(text) > 20_000:
                raise DesktopError("Text must be a string of at most 20,000 characters.")
            await el.fill(text, timeout=5000)
            return f"Typed into ({method}): '{description}'"
        return f"Could not find input: '{description}'"

    async def new_tab(self, url: str = "") -> str:
        page = await self._get_page()
        ctx  = page.context
        new  = await ctx.new_page()
        self._page = new
        if url:
            get_service().check_cancelled()
            await new.goto(_normalize_url(url), wait_until="domcontentloaded", timeout=30_000)
            return f"Opened new tab: {_safe_url(new.url)}"
        return "New tab opened."

    async def close_tab(self) -> str:
        page = await self._get_page() if self._page is not None else None
        if page and not page.is_closed():
            ctx   = page.context
            await page.close()
            pages = ctx.pages
            self._page = pages[-1] if pages else None
            return "Tab closed."
        return "No active tab to close."

    async def screenshot(self, path: str = None) -> str:
        from actions.computer_control import _load_config, _safe_screenshot_path
        if not _load_config().get("desktop_capture_enabled", False):
            return "Browser screenshot storage is disabled; enable desktop_capture_enabled locally."
        page = await self._get_page()
        try:
            await self._safe_page(page)
            save_path = str(_safe_screenshot_path(path))
            await page.screenshot(path=save_path, full_page=False)
            return f"Screenshot saved: {save_path}"
        except Exception as e:
            return f"Screenshot error: {_safe_error(e)}"

    async def back(self) -> str:
        page = await self._get_page()
        try:
            await page.go_back(timeout=10_000)
            return f"Navigated back: {_safe_url(page.url)}"
        except Exception as e:
            return f"Back error: {_safe_error(e)}"

    async def forward(self) -> str:
        page = await self._get_page()
        try:
            await page.go_forward(timeout=10_000)
            return f"Navigated forward: {_safe_url(page.url)}"
        except Exception as e:
            return f"Forward error: {_safe_error(e)}"

    async def reload(self) -> str:
        page = await self._get_page()
        try:
            await page.reload(timeout=15_000)
            return f"Page reloaded: {_safe_url(page.url)}"
        except Exception as e:
            return f"Reload error: {_safe_error(e)}"

    async def close_browser(self) -> str:
        await self._async_close()
        return f"{self.browser_name} closed."

class _SessionRegistry:
    """Manages all active browser sessions."""

    def __init__(self):
        self._sessions:        dict[str, _BrowserSession] = {}
        self._active_browser:  str                        = ""
        self._lock             = threading.Lock()
        self._last_native_url: str                        = ""

    def has(self, browser_name: str | None = None) -> bool:
        """Is there an active automation session for this browser (or any)?"""
        with self._lock:
            if not browser_name:
                return bool(self._sessions)
            name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
            return name in self._sessions

    def note_native_url(self, url: str) -> None:
        self._last_native_url = url

    def pop_native_url(self) -> str:
        """Returns the last natively-opened URL once (consumed to avoid repeats)."""
        url, self._last_native_url = self._last_native_url, ""
        return url

    def _get_or_create(self, browser_name: str) -> _BrowserSession:
        with self._lock:
            if browser_name not in self._sessions:
                sess = _BrowserSession(browser_name)
                sess.start()
                self._sessions[browser_name] = sess
                print(f"[Registry] New session: {browser_name}")
            return self._sessions[browser_name]

    def get(self, browser_name: str | None = None) -> _BrowserSession:
        if not browser_name:
            browser_name = self._active_browser or ("chrome" if _cdp_endpoint() else _detect_default_browser())
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        sess = self._get_or_create(browser_name)
        self._active_browser = browser_name
        return sess

    def switch(self, browser_name: str) -> str:
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        self._get_or_create(browser_name)
        self._active_browser = browser_name
        return f"Active browser → {browser_name}"

    def close_one(self, browser_name: str) -> str:
        with self._lock:
            sess = self._sessions.pop(browser_name, None)
        if sess:
            sess.close()
            if self._active_browser == browser_name:
                self._active_browser = ""
            return f"{browser_name} closed."
        return f"No active session for: {browser_name}"

    def close_all(self) -> str:
        with self._lock:
            names    = list(self._sessions.keys())
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._active_browser = ""
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass
        return "All browsers closed: " + (", ".join(names) if names else "none")

    def list_sessions(self) -> str:
        with self._lock:
            if not self._sessions:
                return "No active browser sessions."
            lines = []
            for name in self._sessions:
                CHIDVIer = " ◀ active" if name == self._active_browser else ""
                lines.append(f"  • {name}{CHIDVIer}")
            return "Open browsers:\n" + "\n".join(lines)


_registry = _SessionRegistry()

def browser_control(
    parameters:    dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params  = parameters or {}
    action  = params.get("action", "").lower().strip()
    browser = params.get("browser", "").lower().strip() or None
    result  = "Unknown action."

    if action == "switch":
        target = browser or params.get("target", "").lower().strip()
        result = _registry.switch(target) if target else "Please specify a browser."
        _log(player, result)
        return result

    if action == "list_browsers":
        result = _registry.list_sessions()
        _log(player, result)
        return result

    if action == "close_all":
        result = _registry.close_all()
        _log(player, result)
        return result

    if action == "close":
        target = browser or _registry._active_browser
        result = _registry.close_one(target) if target else "No browser specified."
        _log(player, result)
        return result

    # ── Navigation is ALWAYS native ──────────────────────────────────────────
    # go_to / search / new_tab open the site in the user's own browser —
    # their own profile, logged-in accounts and start page; exactly as if the
    # user had opened it themselves. A controlled window with about:blank never
    # opens here. The only exception: if an automation flow is already running,
    # navigation continues in that window (so multi-step tasks aren't split).
    if action in ("go_to", "search", "new_tab"):
        if _registry.has(browser) or _cdp_endpoint():
            sess = _registry.get(browser)
            try:
                if action == "search":
                    result = sess.run(sess.search(params.get("query", ""),
                                                  params.get("engine", "google")))
                elif action == "new_tab":
                    result = sess.run(sess.new_tab(params.get("url", "")))
                else:
                    result = sess.run(sess.go_to(params.get("url", "")))
            except concurrent.futures.TimeoutError:
                result = f"Browser action '{action}' timed out (60s)."
            except Exception as e:
                result = f"Browser error ({action}): {_safe_error(e)}"
            _log(player, result)
            return result

        if action == "search":
            base    = _SEARCH_ENGINES.get(params.get("engine", "google").lower(),
                                          _SEARCH_ENGINES["google"])
            nav_url = base + params.get("query", "").replace(" ", "+")
        else:
            nav_url = params.get("url", "").strip()

        result = _open_native(nav_url, browser)
        if result.startswith("Opened") and nav_url:
            _registry.note_native_url(_normalize_url(nav_url))
        _log(player, result)
        return result

    # ── Interactive actions (click/type/read…) ───────────────────────────────
    # These require a physically controllable browser; the automation window
    # only opens here, and as soon as it opens it goes to the user's last
    # navigated page — it doesn't sit on a blank page.
    try:
        sess = _registry.get(browser)
    except Exception as e:
        result = f"Could not start browser session: {_safe_error(e)}"
        _log(player, result)
        return result

    try:
        last = "" if _cdp_endpoint() else _registry.pop_native_url()
        if last:
            try:
                sess.run(sess.go_to(last))
            except Exception as e:
                print(f"[Browser] Could not resume last page ({_safe_url(last)}): {_safe_error(e)}")

        if action in ("inspect", "observe"):
            result = json.dumps(sess.run(sess.inspect()))
        elif action == "click":
            result = sess.run(sess.click(params.get("selector"), params.get("text")))
        elif action == "type":
            result = sess.run(sess.type_text(
                params.get("selector"), params.get("text", ""), params.get("clear_first", True)))
        elif action == "scroll":
            result = sess.run(sess.scroll(params.get("direction", "down"), int(params.get("amount", 500))))
        elif action == "fill_form":
            result = sess.run(sess.fill_form(params.get("fields", {})))
        elif action == "smart_click":
            result = sess.run(sess.smart_click(params.get("description", "")))
        elif action == "smart_type":
            result = sess.run(sess.smart_type(params.get("description", ""), params.get("text", "")))
        elif action == "get_text":
            result = sess.run(sess.get_text())
        elif action == "get_url":
            result = sess.run(sess.get_url())
        elif action == "press":
            result = sess.run(sess.press(params.get("key", "Enter")))
        elif action == "close_tab":
            result = sess.run(sess.close_tab())
        elif action == "screenshot":
            result = sess.run(sess.screenshot(params.get("path")))
        elif action == "back":
            result = sess.run(sess.back())
        elif action == "forward":
            result = sess.run(sess.forward())
        elif action == "reload":
            result = sess.run(sess.reload())
        else:
            result = f"Unknown browser action: '{action}'"

    except concurrent.futures.TimeoutError:
        result = f"Browser action '{action}' timed out (60s)."
    except Exception as e:
        result = f"Browser error ({action}): {_safe_error(e)}"

    _log(player, result)
    return result


def _log(player, text: str):
    short = "Browser request completed; result available in current task."
    print(f"[Browser] {short}")
    if player:
        player.write_log(f"[browser] {short[:60]}")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "browser_control",
    "description": "Browser control. CHIDVI_CHROME_CDP explicitly attaches to a loopback debug-enabled Chrome instance. Otherwise navigation opens the normal browser and interaction opens a separate CHIDVI automation profile; it cannot inspect your existing normal tabs. inspect returns visible main-document controls without form values. Clicks require exact speech and a visible ring; sensitive actions require desktop confirmation.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "inspect | go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all. inspect/get_text omit form values. Sensitive clicks require desktop confirmation; credentials/challenges require manual input."
            },
            "browser": {
                "type": "STRING",
                "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."
            },
            "url": {
                "type": "STRING",
                "description": "URL for go_to / new_tab action"
            },
            "query": {
                "type": "STRING",
                "description": "Search query for search action"
            },
            "engine": {
                "type": "STRING",
                "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"
            },
            "selector": {
                "type": "STRING",
                "description": "CSS selector for click/type"
            },
            "text": {
                "type": "STRING",
                "description": "Text to click or type"
            },
            "description": {
                "type": "STRING",
                "description": "Element description for smart_click/smart_type"
            },
            "direction": {
                "type": "STRING",
                "description": "up | down for scroll"
            },
            "amount": {
                "type": "INTEGER",
                "description": "Scroll amount in pixels (default: 500)"
            },
            "key": {
                "type": "STRING",
                "description": "Key name for press action (e.g. Enter, Escape, F5)"
            },
            "path": {
                "type": "STRING",
                "description": "Save path for screenshot"
            },
            "incognito": {
                "type": "BOOLEAN",
                "description": "Open in private/incognito mode"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": browser_control,
}
