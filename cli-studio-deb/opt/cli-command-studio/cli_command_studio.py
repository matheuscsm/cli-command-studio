#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI Command Studio (versão Python)

Interface gráfica para yt-dlp, FFmpeg, Instaloader e spotDL que EXECUTA os
comandos por você, mostrando a saída ao vivo. Sem prompt, sem copiar e colar.

Requisitos: Python 3.8+ com tkinter (Windows/macOS já vêm com ele; no Linux
instale o pacote python3-tk) e as ferramentas que quiser usar (yt-dlp, ffmpeg,
spotdl, instaloader). No Linux o app também procura em ~/.local/bin (pipx).

Uso:      python3 cli_command_studio.py
Portátil: menu Ferramentas -> baixa yt-dlp e ffmpeg para tools/<sistema>/ ao lado do app
          (ou:  python3 cli_command_studio.py --setup-tools [--all])
Linux:    python3 cli_command_studio.py --install-launcher   (cria o atalho no menu)
HiDPI:    STUDIO_SCALE=1.5 python3 cli_command_studio.py     (aumenta a interface)
"""
from __future__ import annotations

import codecs
import importlib.util
import os
import platform
import queue
import re
import shlex
from urllib.parse import urlsplit
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def pkg_manager():
    """(gerenciador, comando de instalação) da distro Linux, lendo /etc/os-release."""
    ids = ""
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith(("ID=", "ID_LIKE=")):
                ids += " " + line.split("=", 1)[1].strip().strip('"').lower()
    except OSError:
        pass
    words = ids.split()
    if {"debian", "ubuntu", "linuxmint", "pop", "raspbian"} & set(words):
        return "apt", "sudo apt install"
    if {"fedora", "rhel", "centos", "rocky", "almalinux"} & set(words):
        return "dnf", "sudo dnf install"
    if {"arch", "manjaro", "endeavouros", "cachyos"} & set(words):
        return "pacman", "sudo pacman -S"
    if {"suse", "opensuse", "opensuse-leap", "opensuse-tumbleweed"} & set(words):
        return "zypper", "sudo zypper install"
    return "", "sudo <gerenciador-da-sua-distro> install"


try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    if IS_WIN:
        sys.exit("O tkinter não está instalado. Reinstale o Python marcando a opção 'tcl/tk and IDLE'.")
    _pm, _cmd = pkg_manager()
    _pkg = {"apt": "python3-tk", "dnf": "python3-tkinter", "pacman": "tk", "zypper": "python3-tk"}.get(_pm, "python3-tk")
    sys.exit("O módulo tkinter (a interface gráfica do Python) não está instalado.\n"
             f"Instale com:  {_cmd} {_pkg}\n"
             "e rode o app de novo.")

PY_MODULES = {"yt-dlp": "yt_dlp", "spotdl": "spotdl", "instaloader": "instaloader"}


def install_hint(tool: str) -> str:
    """Como instalar a ferramenta neste sistema."""
    if IS_WIN:
        return "winget install ffmpeg" if tool == "ffmpeg" else f"pip install -U {tool}"
    if IS_MAC:
        return "brew install ffmpeg" if tool == "ffmpeg" else f"pipx install {tool}   (ou: brew install {tool})"
    pm, cmd = pkg_manager()
    if tool == "ffmpeg":
        return f"{cmd} ffmpeg" + ("   (requer o repositório RPM Fusion)" if pm == "dnf" else "")
    pipx = {"apt": "pipx", "dnf": "pipx", "pacman": "python-pipx", "zypper": "python3-pipx"}.get(pm, "pipx")
    return f"{cmd} {pipx} && pipx ensurepath && pipx install {tool}"


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def extra_bin_dirs() -> list:
    """Pastas onde pip/pipx costumam instalar programas e que nem sempre estão no PATH do app."""
    exe_dir = Path(sys.executable).parent
    dirs = [exe_dir]
    if IS_WIN:
        dirs.append(exe_dir / "Scripts")
        if os.environ.get("APPDATA"):
            v = f"Python{sys.version_info.major}{sys.version_info.minor}"
            dirs.append(Path(os.environ["APPDATA"]) / "Python" / v / "Scripts")
    else:
        dirs += [Path.home() / ".local" / "bin", Path.home() / "bin", Path("/usr/local/bin"),
                 Path("/snap/bin"), Path("/opt/homebrew/bin")]
    if os.environ.get("APPDIR"):  # AppImage: ferramentas embutidas (yt-dlp/ffmpeg), se houver
        dirs.append(Path(os.environ["APPDIR"]) / "usr" / "bin")
    return [d for d in dirs if d.is_dir()]


def default_workdir() -> Path:
    """Pasta Downloads do usuário."""
    dl = Path.home() / "Downloads"
    return dl if dl.is_dir() else Path.home()


# ---------------- versão portátil: tools/<sistema>/ ao lado do app ----------------
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/{}"
TOOL_SOURCES = {
    "linux-x86_64": dict(
        ytdlp="yt-dlp_linux",
        ffmpeg=[("tar", "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz")]),
    "linux-aarch64": dict(
        ytdlp="yt-dlp_linux_aarch64",
        ffmpeg=[("tar", "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz")]),
    "windows-x86_64": dict(
        ytdlp="yt-dlp.exe",
        ffmpeg=[("zip", "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
                ("zip", "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                        "ffmpeg-master-latest-win64-gpl.zip")]),
}
SHARED_PLATFORMS = ["windows-x86_64", "linux-x86_64"]


class Cancelled(Exception):
    pass


def plat_key() -> str:
    m = platform.machine().lower()
    arch = "aarch64" if m in ("aarch64", "arm64") else "x86_64" if m in ("x86_64", "amd64") else m
    return f"{'windows' if IS_WIN else 'macos' if IS_MAC else 'linux'}-{arch}"


def portable_src(plat: str = "") -> Path:
    return app_dir() / "tools" / (plat or plat_key())


_PORTABLE = None  # cache: Path, "" (não há) ou None (ainda não calculado)


def reset_portable_cache():
    global _PORTABLE
    _PORTABLE = None


def _sync_to_cache(files, cache: Path):
    """Copia as ferramentas para uma pasta local executável (NTFS/exFAT costumam bloquear execução)."""
    cache.mkdir(parents=True, exist_ok=True)
    for f in files:
        dst = cache / f.name
        st = f.stat()
        if not dst.exists() or dst.stat().st_size != st.st_size or abs(dst.stat().st_mtime - st.st_mtime) > 2:
            print(f"Copiando {f.name} para {cache} (só na primeira vez)…", file=sys.stderr)
            shutil.copy2(f, dst)
        dst.chmod(0o755)
    return cache if all(os.access(cache / f.name, os.X_OK) for f in files) else None


def portable_dir():
    """Pasta pronta para executar com as ferramentas portáteis deste sistema, ou None."""
    global _PORTABLE
    if _PORTABLE is None:
        _PORTABLE = ""
        src = portable_src()
        if src.is_dir():
            files = [f for f in src.iterdir() if f.is_file() and not f.name.endswith(".part")]
            if files:
                _PORTABLE = src
                if not IS_WIN and any(not os.access(f, os.X_OK) for f in files):
                    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
                    _PORTABLE = _sync_to_cache(files, cache_root / "cli-command-studio" / "tools" / plat_key()) or ""
    return _PORTABLE or None


def _download(url, dest: Path, log, progress, stop):
    req = urllib.request.Request(url, headers={"User-Agent": "cli-command-studio"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = last = 0
        while True:
            if stop.is_set():
                raise Cancelled()
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if done - last >= 1_000_000:
                last = done
                txt = f"{done / 1e6:.0f}/{total / 1e6:.0f} MB" if total else f"{done / 1e6:.0f} MB"
                progress(done * 100 / total if total else 0, txt)
                log("  " + txt, None, True)


def _write_stream(fsrc, dst: Path, exe: bool):
    part = dst.with_name(dst.name + ".part")
    with open(part, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst)
    if exe:
        part.chmod(0o755)
    os.replace(part, dst)


def _extract_ffmpeg(archive: Path, kind: str, dest: Path, ext: str):
    wanted, found = {"ffmpeg" + ext, "ffprobe" + ext}, set()
    if kind == "zip":
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                name = m.filename.rsplit("/", 1)[-1]
                if name in wanted and "/bin/" in "/" + m.filename:
                    with z.open(m) as fsrc:
                        _write_stream(fsrc, dest / name, False)
                    found.add(name)
    else:
        with tarfile.open(archive, "r:xz") as t:
            for m in t:
                name = os.path.basename(m.name)
                if m.isfile() and name in wanted:
                    _write_stream(t.extractfile(m), dest / name, True)
                    found.add(name)
    if found != wanted:
        raise RuntimeError("ffmpeg/ffprobe não encontrados no arquivo baixado")


def setup_platform(plat: str, log, progress, stop, force_ffmpeg: bool = False):
    """Baixa yt-dlp (sempre a versão mais nova) e, se faltar, ffmpeg/ffprobe para tools/<plat>/."""
    src = TOOL_SOURCES.get(plat)
    if not src:
        raise RuntimeError(f"sem download automático para '{plat}'")
    win = plat.startswith("windows")
    ext = ".exe" if win else ""
    dest = portable_src(plat)
    dest.mkdir(parents=True, exist_ok=True)

    log(f"[{plat}] baixando yt-dlp…", None, False)
    part = dest / ("yt-dlp" + ext + ".part")
    try:
        _download(YTDLP_URL.format(src["ytdlp"]), part, log, progress, stop)
        if not win:
            part.chmod(0o755)
        os.replace(part, dest / ("yt-dlp" + ext))
    finally:
        part.unlink(missing_ok=True)

    if all((dest / (n + ext)).is_file() for n in ("ffmpeg", "ffprobe")) and not force_ffmpeg:
        log(f"[{plat}] ffmpeg já está na pasta — mantido.", None, False)
        return
    err = None
    for kind, url in src["ffmpeg"]:
        log(f"[{plat}] baixando ffmpeg…", None, False)
        archive = dest / "ffmpeg.download.part"
        try:
            _download(url, archive, log, progress, stop)
            _extract_ffmpeg(archive, kind, dest, ext)
            err = None
            break
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            err = e
            log(f"  falhou ({e}); tentando outra fonte…", "warn", False)
        finally:
            archive.unlink(missing_ok=True)
    if err:
        raise err


def resolve_tool(name: str):
    """Prefixo de comando para executar a ferramenta (PATH ou `python -m`), ou None."""
    pd = portable_dir()
    path = ((shutil.which(name, path=str(pd)) if pd else None) or shutil.which(name)
            or shutil.which(name, path=os.pathsep.join(map(str, extra_bin_dirs()))))
    if path:
        return [path]
    mod = PY_MODULES.get(name)
    if mod and not getattr(sys, "frozen", False) and importlib.util.find_spec(mod):
        return [sys.executable, "-m", mod]
    return None


def app_dir() -> Path:
    """Pasta onde o app está (script .py ou executável empacotado)."""
    if os.environ.get("STUDIO_HOME"):  # força a pasta do app (útil em scripts de build/teste)
        return Path(os.environ["STUDIO_HOME"]).resolve()
    appimage = os.environ.get("APPIMAGE")  # caminho do arquivo .AppImage em execução
    if appimage:
        return Path(appimage).resolve().parent
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_ffmpeg_dir(custom: str = "") -> str:
    """Pasta que contém o ffmpeg: campo manual, PATH, pasta do app e locais comuns. '' se não achar."""
    exe = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    cands = []
    if custom:
        c = Path(custom).expanduser()
        cands += [c if c.is_dir() else c.parent]
    pd = portable_dir()
    if pd:
        cands.append(pd)
    w = shutil.which("ffmpeg")
    if w:
        cands.append(Path(w).parent)
    base = app_dir()
    cands += [base, base / "bin", base / "ffmpeg", base / "ffmpeg" / "bin"]
    if IS_WIN:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pd = os.environ.get("ProgramData", r"C:\ProgramData")
        cands += [Path(r"C:\ffmpeg\bin"), Path(pf) / "ffmpeg" / "bin",
                  Path(pd) / "chocolatey" / "bin", Path.home() / "scoop" / "shims"]
        la = os.environ.get("LOCALAPPDATA")
        if la:
            wg = Path(la) / "Microsoft" / "WinGet"
            cands.append(wg / "Links")
            try:
                cands += [f.parent for f in (wg / "Packages").glob("*FFmpeg*/**/ffmpeg.exe")]
            except OSError:
                pass
    else:
        cands += [Path.home() / ".local" / "bin", Path("/snap/bin"), Path("/opt/homebrew/bin"),
                  Path("/usr/local/bin"), Path("/usr/bin")]
    cands += extra_bin_dirs()
    for d in cands:
        try:
            if (d / exe).is_file():
                return str(d)
        except OSError:
            continue
    return ""


_SAFE_ARG = re.compile(r"[\w@%+=:,./-]+")


def _quote_portable(a: str) -> str:
    """Aspas que funcionam ao colar tanto no CMD/PowerShell quanto no bash (Termux/Linux).
    list2cmdline só protege argumentos com espaço e deixa `<`, `>`, `*`, `[`, `]`, `&`
    soltos (ex.: bv*[height<=720]+ba/...), o que quebra no bash."""
    if a and _SAFE_ARG.fullmatch(a):
        return a
    if any(c in a for c in '"\\$`'):
        return subprocess.list2cmdline([a]) if IS_WIN else shlex.quote(a)
    return f'"{a}"'


def fmt_cmd(tool: str, args, secrets=()) -> str:
    parts = [tool] + ["********" if a in secrets else a for a in args]
    return " ".join(_quote_portable(p) for p in parts) if IS_WIN else shlex.join(parts)


def kill_tree(proc: subprocess.Popen):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def looks_like_playlist(url: str) -> bool:
    """Palpite offline (para o botão Copiar): o link parece uma playlist/álbum?"""
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    path = u.path.lower()
    return ("list=" in u.query.lower()
            or any(k in path for k in ("/playlist", "/playlists", "/album", "/sets/")))


@dataclass
class Job:
    tool: str
    args: list
    missing: str = ""        # mensagem se faltar algum dado obrigatório
    tee: str = ""            # arquivo que recebe o stdout (spotdl url)
    secrets: tuple = ()      # valores mascarados no preview/log
    batch: dict | None = None  # FFmpeg em lote
    auto: dict | None = None   # yt-dlp com detecção automática de playlist

    def _batch_args(self, src: str, out: str):
        b = self.batch
        return ["-hide_banner", "-nostdin", b["ow"], "-i", src, *b["opts"], out]

    def preview(self) -> str:
        if self.auto:
            a = self.auto
            args = a["pre"] + ["-o", a["single_o"]] + a["post"] + (["-a", a["batch"]] if a["batch"] else a["urls"])
            return (fmt_cmd(self.tool, args)
                    + "\n# Se o link for uma playlist, a pasta dela é criada automaticamente:"
                    + f"\n#   -o {shlex.quote(a['playlist_o'])}"
                    + ("\n# (lista TXT: cada link é verificado e baixado separadamente)" if a["batch"] else ""))
        if self.batch:
            b = self.batch
            args = self._batch_args(f"<arquivo>.{b['in_ext']}", f"<arquivo>{b['suffix']}.{b['out_ext']}")
            return fmt_cmd(self.tool, args) + f"\n# repetido para cada *.{b['in_ext']} da pasta de destino"
        text = fmt_cmd(self.tool, self.args, self.secrets)
        if self.tee:
            text += f"\n# a saída (links) também é gravada em {self.tee}"
        return text

    def copy_text(self) -> str:
        """Comando para colar em outro terminal. No modo automático o programa detecta a
        playlist sozinho ao executar, mas um comando copiado não consegue — então escolhe o
        modelo (com pasta de álbum/playlist ou de vídeo único) pelo formato do link."""
        if self.auto and not self.auto["batch"]:
            a = self.auto
            playlist = "--no-playlist" not in a["pre"] and any(looks_like_playlist(u) for u in a["urls"])
            tpl = a["playlist_o"] if playlist else a["single_o"]
            return fmt_cmd(self.tool, a["pre"] + ["-o", tpl] + a["post"] + a["urls"])
        return self.preview().splitlines()[0]

    def expand(self, cwd: Path):
        """-> (lista de comandos [args], avisos)"""
        if not self.batch:
            return [self.args], []
        b = self.batch
        cmds, warns = [], []
        for f in sorted(Path(cwd).glob(f"*.{b['in_ext']}")):
            out = f"{f.stem}{b['suffix']}.{b['out_ext']}"
            if out.lower() == f.name.lower():
                warns.append(f"Pulado (saída = entrada): {f.name} — use um sufixo ou outra extensão.")
                continue
            cmds.append(self._batch_args(f.name, out))
        return cmds, warns


def audio_args(fmt: str, br: str, keep_cover: bool) -> list:
    a = (["-map", "0:a", "-map", "0:v:0?", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
         if keep_cover else ["-map", "0:a"])
    a += ["-map_metadata", "0"]
    if fmt == "mp3":
        a += ["-c:a", "libmp3lame", "-b:a", br, "-id3v2_version", "3", "-write_id3v1", "1"]
    elif fmt == "m4a":
        a += ["-c:a", "aac", "-b:a", br]
    elif fmt == "flac":
        a += ["-c:a", "flac", "-compression_level", "8"]
    return a


class ScrollFrame(ttk.Frame):
    """Área com rolagem vertical (barra + roda do mouse); o conteúdo vai em `.body`."""

    def __init__(self, parent):
        super().__init__(parent)
        bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=bg, yscrollincrement=24)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=6)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))

    def _on_body(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self.body.winfo_reqheight() <= self.canvas.winfo_height():
            self.canvas.yview_moveto(0)

    def scroll(self, steps: int):
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(steps, "units")


# --------------------------------------------------------------------------
# Aplicação
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__(className="CliCommandStudio")
        scale = os.environ.get("STUDIO_SCALE")
        if scale:
            try:
                self.tk.call("tk", "scaling", float(scale) * 96 / 72)
            except (ValueError, tk.TclError):
                pass
        self.title("CLI Command Studio — Python")
        self.geometry("1100x920")
        self.minsize(960, 700)

        # Carrega o ícone da janela no Linux
        icon_path = Path("/usr/share/pixmaps/cli-command-studio.png")
        if icon_path.is_file():
            try:
                self._app_icon = tk.PhotoImage(file=str(icon_path))
                self.iconphoto(True, self._app_icon)
            except Exception:
                pass

        self.v: dict[str, tk.Variable] = {}
        self._ready = False
        self.q: queue.Queue = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self._last_progress = False
        self.ff_panels: dict[str, ttk.Frame] = {}

        style = ttk.Style(self)
        try:
            style.theme_use("vista" if IS_WIN else "clam")
        except tk.TclError:
            pass
        style.configure("Run.TButton", font=("TkDefaultFont", 10, "bold"))

        self.bind_all("<MouseWheel>", self.on_wheel)
        self.bind_all("<Button-4>", self.on_wheel)
        self.bind_all("<Button-5>", self.on_wheel)
        # a roda não deve trocar a opção de um combobox por acidente: rola a tela
        self.bind_class("TCombobox", "<MouseWheel>", self.on_wheel_combo)
        self.bind_class("TCombobox", "<Button-4>", self.on_wheel_combo)
        self.bind_class("TCombobox", "<Button-5>", self.on_wheel_combo)

        self.build_ui()
        self._ready = True
        self.refresh()
        self.after(60, self.poll)

    def on_wheel(self, e):
        if getattr(e, "num", 0) == 4:
            steps = -2
        elif getattr(e, "num", 0) == 5:
            steps = 2
        elif sys.platform == "darwin":
            steps = -int(e.delta) or (-1 if e.delta > 0 else 1)
        else:
            steps = -2 * (int(e.delta / 120) or (1 if e.delta > 0 else -1))
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except Exception:  # noqa: BLE001  (popdown do combobox)
            return
        while w is not None:
            if isinstance(w, ScrollFrame):
                w.scroll(steps)
                return
            w = getattr(w, "master", None)

    def on_wheel_combo(self, e):
        self.on_wheel(e)
        return "break"

    # ---------------- helpers de variáveis / widgets ----------------
    def var(self, name, default="", kind="s"):
        v = (tk.BooleanVar if kind == "b" else tk.StringVar)(value=default)
        v.trace_add("write", lambda *_: self.refresh())
        self.v[name] = v
        return v

    def val(self, name) -> str:
        return str(self.v[name].get()).strip()

    def chk(self, name) -> bool:
        return bool(self.v[name].get())

    @staticmethod
    def show(w, cond: bool):
        if cond:
            if not w.winfo_manager():
                w.pack(fill="x")
        else:
            w.pack_forget()

    def card(self, parent, title, side=None):
        f = ttk.LabelFrame(parent, text=title, padding=8)
        if side:
            f.pack(side=side, fill="both", expand=True, padx=3, pady=3)
        else:
            f.pack(fill="x", padx=3, pady=3)
        return f

    def slot(self, parent):
        s = ttk.Frame(parent)
        s.pack(fill="x")
        return s

    def stack(self, parent):
        """Contêiner onde várias 'páginas' ocupam o mesmo lugar; só a de cima aparece (tkraise)."""
        f = ttk.Frame(parent)
        f.pack(fill="x")
        f.columnconfigure(0, weight=1)
        return f

    @staticmethod
    def page(stk):
        p = ttk.Frame(stk)
        p.grid(row=0, column=0, sticky="nsew")
        return p

    def cols(self, parent, n):
        f = ttk.Frame(parent)
        f.pack(fill="x")
        out = []
        for i in range(n):
            c = ttk.Frame(f)
            c.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else 8, 0))
            out.append(c)
        return out

    def lbl_entry(self, parent, label, name, default="", browse=None, show=None):
        ttk.Label(parent, text=label).pack(anchor="w", pady=(6, 0))
        line = ttk.Frame(parent)
        line.pack(fill="x")
        ttk.Entry(line, textvariable=self.var(name, default), show=show).pack(side="left", fill="x", expand=True)
        if browse:
            ttk.Button(line, text="…", width=3,
                       command=lambda: self.browse(name, browse)).pack(side="left", padx=(4, 0))

    def lbl_combo(self, parent, label, name, options, default, on_change=None):
        ttk.Label(parent, text=label).pack(anchor="w", pady=(6, 0))
        v = self.var(name, default)
        to_val = {lab: val for val, lab in options}
        to_lab = {val: lab for val, lab in options}
        cb = ttk.Combobox(parent, state="readonly", values=[lab for _, lab in options])
        cb.set(to_lab[default])
        cb.pack(fill="x")

        def selected(_e):
            v.set(to_val[cb.get()])
            if on_change:
                on_change()
            self.refresh()
        cb.bind("<<ComboboxSelected>>", selected)

    def lbl_check(self, parent, label, name, default=False):
        ttk.Checkbutton(parent, text=label, variable=self.var(name, default, "b")).pack(anchor="w", pady=2)

    def radios(self, parent, name, options, default, command=None):
        v = self.var(name, default)
        for val, lab in options:
            ttk.Radiobutton(parent, text=lab, value=val, variable=v, command=command).pack(anchor="w", pady=1)

    def meta_fields(self, parent, prefix):
        c = self.cols(parent, 3)
        self.lbl_entry(c[0], "Artista / Banda / Criador:", f"{prefix}_artist")
        self.lbl_entry(c[1], "Artista do Álbum:", f"{prefix}_albumartist")
        self.lbl_entry(c[2], "Álbum:", f"{prefix}_album")
        c = self.cols(parent, 3)
        self.lbl_entry(c[0], "Título (só arquivo único):", f"{prefix}_title")
        self.lbl_entry(c[1], "Ano (ex: 2024):", f"{prefix}_year")
        self.lbl_entry(c[2], "Gênero:", f"{prefix}_genre")

    def browse(self, name, kind):
        if kind == "dir":
            p = filedialog.askdirectory()
        elif kind == "save":
            p = filedialog.asksaveasfilename()
        else:
            p = filedialog.askopenfilename()
        if not p:
            return
        self.v[name].set(p)
        if name == "ff_in":  # sugere um nome de saída ao lado do arquivo escolhido
            ext = self.val("ff_out").rsplit(".", 1)[-1] if "." in self.val("ff_out") else "mp4"
            self.v["ff_out"].set(str(Path(p).with_name(Path(p).stem + "_saida." + ext)))

    # ---------------- construção da interface ----------------
    def build_ui(self):
        menubar = tk.Menu(self)
        mt = tk.Menu(menubar, tearoff=False)
        mt.add_command(label="Baixar/atualizar ferramentas (este sistema)",
                       command=lambda: self.start_setup(False))
        mt.add_command(label="Baixar/atualizar para Windows e Linux (pasta compartilhada)",
                       command=lambda: self.start_setup(True))
        mt.add_separator()
        mt.add_command(label="Abrir pasta das ferramentas", command=lambda: self.open_folder(portable_src()))
        menubar.add_cascade(label="Ferramentas", menu=mt)
        self.config(menu=menubar)

        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=8, pady=8)

        self.nb = ttk.Notebook(pw)
        pw.add(self.nb, weight=3)
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self.refresh())
        for title, builder in (("  yt-dlp  ", self.tab_yt), ("  FFmpeg  ", self.tab_ff),
                               ("  Instaloader  ", self.tab_in), ("  spotDL  ", self.tab_sp)):
            f = ScrollFrame(self.nb)
            self.nb.add(f, text=title)
            builder(f.body)

        bottom = ttk.Frame(pw, padding=(0, 8, 0, 0))
        pw.add(bottom, weight=2)
        self.bottom_ui(bottom)

    # ----- yt-dlp -----
    def tab_yt(self, tab):
        top = self.card(tab, "Links")
        c = self.cols(top, 2)
        ttk.Label(c[0], text="Link(s) do vídeo ou playlist (um por linha):").pack(anchor="w")
        self.yt_text = tk.Text(c[0], height=3, wrap="word", undo=True)
        self.yt_text.pack(fill="x")
        for ev in ("<KeyRelease>", "<<Paste>>", "<<Cut>>"):
            self.yt_text.bind(ev, lambda _e: self.after(20, self.refresh))
        self.lbl_entry(c[1], "Ou arquivo TXT com links (-a):", "yt_batch", browse="file")

        row = ttk.Frame(tab)
        row.pack(fill="x")

        a = self.card(row, "Modo", "left")
        self.radios(a, "yt_mode", [("video", "Vídeo completo"), ("audio", "Apenas áudio (converter)"),
                                   ("thumb", "Apenas capa (thumbnail)")], "video")
        self.var("yt_strict", False, "b")
        stk = self.stack(a)
        self.pg_video = self.page(stk)
        self.lbl_combo(self.pg_video, "Teto de resolução:", "yt_res",
                       [("best", "Melhor disponível"), ("2160", "Máx 4K (2160p)"), ("1440", "Máx 2K (1440p)"),
                        ("1080", "Máx 1080p"), ("720", "Máx 720p"), ("480", "480p")], "1080")
        self.lbl_combo(self.pg_video, "Formato:", "yt_cont",
                       [("mp4", "MP4"), ("mkv", "MKV"), ("webm", "WebM")], "mp4")
        ttk.Checkbutton(self.pg_video, text="Preferir fontes MP4/M4A",
                        variable=self.v["yt_strict"]).pack(anchor="w", pady=(8, 2))

        self.pg_audio = self.page(stk)
        self.lbl_combo(self.pg_audio, "Formato do áudio:", "yt_afmt",
                       [("m4a", "M4A (AAC)"), ("mp3", "MP3"), ("opus", "Opus"), ("flac", "FLAC"), ("wav", "WAV")], "m4a")
        self.lbl_combo(self.pg_audio, "Qualidade:", "yt_aq",
                       [("0", "0 (melhor VBR / ~320 kbps)"), ("320K", "320 kbps"), ("256K", "256 kbps"),
                        ("192K", "192 kbps"), ("128K", "128 kbps"), ("2", "2 (~192 kbps)"), ("5", "5 (~128 kbps)")], "0")
        sub = self.stack(self.pg_audio)   # o checkbox só aparece quando o formato NÃO é m4a
        self.pg_strict_on = self.page(sub)
        ttk.Checkbutton(self.pg_strict_on, text="Preferir fonte M4A (menos conversão)",
                        variable=self.v["yt_strict"]).pack(anchor="w", pady=(8, 2))
        self.pg_strict_off = self.page(sub)

        self.pg_thumb = self.page(stk)

        b = self.card(row, "Playlist & Trechos", "left")
        self.lbl_check(b, "Baixar apenas o vídeo único (--no-playlist)", "yt_noplaylist", True)
        self.yt_pl_note = ttk.Label(self.slot(b), foreground="#b45309", wraplength=260,
                                    text="Este link é de um vídeo dentro de uma playlist. "
                                         "Para baixar a playlist inteira, desmarque a opção acima.")
        self.lbl_entry(b, "Itens da playlist (ex: 1-10 ou 2,5,8):", "yt_items")
        self.lbl_entry(b, "Cortar seção (ex: *00:01:00-00:02:30):", "yt_section")
        self.lbl_entry(b, "Pasta de destino extra (opcional):", "yt_folder")
        self.lbl_combo(b, "Nome do arquivo:", "yt_name",
                       [("auto", "Automático (playlist → pasta própria)"),
                        ("artist", "Artista/Canal - Título"), ("title", "Só o título"),
                        ("default", "Padrão do yt-dlp (Título [código])")], "auto")
        self.lbl_entry(b, "Limite de velocidade (ex: 5M):", "yt_rate")

        c = self.card(row, "Extras & Legendas", "left")
        self.lbl_check(c, "Baixar legendas", "yt_subs")
        st = self.stack(c)
        self.pg_embsubs_on = self.page(st)
        ttk.Checkbutton(self.pg_embsubs_on, text="Embutir legenda",
                        variable=self.var("yt_embed_subs", False, "b")).pack(anchor="w", pady=2)
        self.pg_embsubs_off = self.page(st)
        self.lbl_check(c, "Embutir capa no arquivo", "yt_embed_thumb")
        self.lbl_check(c, "Embutir metadados", "yt_embed_meta", True)
        self.lbl_combo(c, "Cookies do navegador (login):", "yt_cookies",
                       [("", "Nenhum"), ("chrome", "Google Chrome"), ("firefox", "Firefox"),
                        ("edge", "Microsoft Edge"), ("brave", "Brave"), ("chromium", "Chromium"),
                        ("vivaldi", "Vivaldi"), ("opera", "Opera")], "")

        self.p_yt_meta = ttk.LabelFrame(self.slot(tab), text="Propriedades do áudio (vazio = mantém o original)", padding=8)
        self.meta_fields(self.p_yt_meta, "yt_m")

    # ----- FFmpeg -----
    def tab_ff(self, tab):
        m = self.card(tab, "Modo de execução")
        line = ttk.Frame(m)
        line.pack(fill="x")
        v = self.var("ff_mode", "single")
        ttk.Radiobutton(line, text="Arquivo único", value="single", variable=v).pack(side="left")
        ttk.Radiobutton(line, text="Em lote (todos *.ext da pasta de destino)", value="batch",
                        variable=v).pack(side="left", padx=12)
        ttk.Checkbutton(line, text="Sobrescrever se já existir",
                        variable=self.var("ff_overwrite", False, "b")).pack(side="right")

        self.p_ff_single = ttk.Frame(self.slot(m))
        c = self.cols(self.p_ff_single, 2)
        self.lbl_entry(c[0], "Arquivo de entrada:", "ff_in", "video.mp4", browse="file")
        self.lbl_entry(c[1], "Arquivo de saída:", "ff_out", "saida.mp4")
        self.p_ff_batch = ttk.Frame(self.slot(m))
        c = self.cols(self.p_ff_batch, 3)
        self.lbl_entry(c[0], "Extensão de origem:", "ff_bin", "mkv")
        self.lbl_entry(c[1], "Extensão de destino:", "ff_bout", "mp4")
        self.lbl_entry(c[2], "Sufixo no nome (opcional):", "ff_bsuffix")

        op = self.card(tab, "Operação")
        bar = ttk.Frame(op)
        bar.pack(fill="x")
        acts = [("convert", "1. Converter"), ("trim", "2. Cortar"), ("compress", "3. Comprimir"),
                ("audio", "4. Extrair áudio"), ("mute", "5. Tirar áudio"), ("resize", "6. Redimensionar"),
                ("gif", "7. Gerar GIF"), ("aconv", "8. Converter áudio")]
        av = self.var("ff_action", "convert")
        for i, (k, lab) in enumerate(acts):
            ttk.Radiobutton(bar, text=lab, value=k, variable=av, style="Toolbutton",
                            command=self.on_ff_action).grid(row=i // 4, column=i % 4, sticky="ew", padx=2, pady=2)
            bar.columnconfigure(i % 4, weight=1)

        def panel(key):
            p = ttk.Frame(self.slot(op))
            self.ff_panels[key] = p
            return p

        p = panel("convert")
        c = self.cols(p, 3)
        self.lbl_combo(c[0], "Formato / contêiner:", "ff_cv_fmt",
                       [("mp4", "MP4"), ("mkv", "MKV"), ("webm", "WebM (VP9)"), ("mov", "MOV"),
                        ("mp3", "MP3 (só áudio)"), ("m4a", "M4A (só áudio)"), ("flac", "FLAC"), ("wav", "WAV")],
                       "mp4", self.sync_ext)
        self.lbl_combo(c[1], "Codec de vídeo:", "ff_cv_vc",
                       [("libx264", "libx264 (H.264)"), ("libx265", "libx265 (HEVC)"), ("copy", "copy (instantâneo)")], "libx264")
        self.lbl_combo(c[2], "Codec de áudio:", "ff_cv_ac",
                       [("aac", "aac"), ("libmp3lame", "libmp3lame (MP3)"), ("copy", "copy")], "aac")

        p = panel("trim")
        c = self.cols(p, 3)
        self.lbl_entry(c[0], "Início (-ss):", "ff_trim_s", "00:00:10")
        self.lbl_entry(c[1], "Fim (-to):", "ff_trim_e", "00:00:45")
        self.lbl_check(c[2], "Usar -c copy (sem perda)", "ff_trim_copy", True)

        p = panel("compress")
        c = self.cols(p, 3)
        self.lbl_entry(c[0], "CRF (18 = alta … 28 = leve):", "ff_comp_crf", "23")
        self.lbl_combo(c[1], "Preset:", "ff_comp_preset",
                       [("ultrafast", "ultrafast"), ("fast", "fast"), ("medium", "medium"), ("slow", "slow")], "medium")
        self.lbl_combo(c[2], "Encoder:", "ff_comp_codec",
                       [("libx264", "H.264 (libx264)"), ("libx265", "H.265 (libx265)")], "libx264")

        p = panel("audio")
        c = self.cols(p, 2)
        self.lbl_combo(c[0], "Modo de extração:", "ff_ea_mode",
                       [("copy", "Cópia direta (-c:a copy)"), ("mp3", "Converter para MP3"),
                        ("aac", "Converter para AAC"), ("flac", "FLAC (lossless)")], "copy", self.sync_ext)
        self.lbl_combo(c[1], "Bitrate (se reencodar):", "ff_ea_br",
                       [("320k", "320 kbps"), ("192k", "192 kbps"), ("128k", "128 kbps")], "320k")

        p = panel("mute")
        ttk.Label(p, text="Remove a trilha de áudio e copia o vídeo sem reencodar (-an -c:v copy).").pack(anchor="w", pady=6)

        p = panel("resize")
        c = self.cols(p, 2)
        self.lbl_combo(c[0], "Dimensão / escala:", "ff_res_scale",
                       [("1920:1080", "Full HD (1920x1080)"), ("1280:720", "HD (1280x720)"),
                        ("854:480", "SD (854x480)"), ("iw/2:ih/2", "50% do original")], "1280:720")
        self.lbl_entry(c[1], "Taxa de quadros (opcional, ex: 30):", "ff_res_fps")

        p = panel("gif")
        c = self.cols(p, 2)
        self.lbl_entry(c[0], "Largura do GIF (px):", "ff_gif_w", "480")
        self.lbl_entry(c[1], "FPS:", "ff_gif_fps", "15")

        p = panel("aconv")
        c = self.cols(p, 3)
        self.lbl_combo(c[0], "Formato de saída:", "ff_ac_fmt",
                       [("mp3", "MP3 (libmp3lame)"), ("m4a", "M4A (AAC)"), ("flac", "FLAC (lossless)")], "m4a", self.sync_ext)
        self.lbl_combo(c[1], "Bitrate (MP3 e M4A):", "ff_ac_br",
                       [("320k", "320 kbps"), ("256k", "256 kbps"), ("192k", "192 kbps"), ("128k", "128 kbps")], "320k")
        self.lbl_check(c[2], "Manter capa embutida", "ff_ac_cover")
        self.meta_fields(p, "ff_ac")

    # ----- Instaloader -----
    def tab_in(self, tab):
        t = self.card(tab, "Alvo (URL do post/reel ou @perfil)")
        self.lbl_entry(t, "Link ou perfil:", "in_target", "https://www.instagram.com/p/DA_exemplo123/")
        row = ttk.Frame(tab)
        row.pack(fill="x")
        a = self.card(row, "Tipo de download", "left")
        self.radios(a, "in_type", [("single", "Publicação / Reel único"), ("profile", "Perfil completo"),
                                   ("stories", "Apenas Stories (requer login)"), ("highlights", "Destaques")], "single")
        b = self.card(row, "Autenticação & limpeza", "left")
        self.lbl_entry(b, "Usuário (login com -l):", "in_login")
        self.lbl_entry(b, "Senha (só em memória; vazio = usar sessão salva):", "in_pass", show="•")
        self.lbl_check(b, "Descartar JSON e .txt extras", "in_clean", True)
        self.lbl_entry(b, "Pasta de destino (--dirname-pattern):", "in_folder")

    # ----- spotDL -----
    def tab_sp(self, tab):
        t = self.card(tab, "Link do Spotify (música, álbum, playlist ou artista)")
        self.lbl_entry(t, "Link:", "sp_url", "https://open.spotify.com/playlist/EXEMPLO")
        a = self.card(tab, "Modo de operação")
        self.radios(a, "sp_mode", [("download", "Baixar direto pelo spotDL"),
                                   ("url_txt", "Apenas exportar links do YouTube para TXT")], "download")
        self.p_sp_txt = ttk.Frame(self.slot(a))
        self.lbl_entry(self.p_sp_txt, "Nome do arquivo TXT de saída:", "sp_txt", "playlist_links.txt")

        self.p_sp_dl = ttk.Frame(self.slot(tab))
        row = ttk.Frame(self.p_sp_dl)
        row.pack(fill="x")
        b = self.card(row, "Formato & qualidade", "left")
        self.lbl_combo(b, "Formato de saída:", "sp_format",
                       [("mp3", "MP3"), ("m4a", "M4A (AAC)"), ("opus", "Opus"), ("flac", "FLAC"),
                        ("ogg", "OGG (Vorbis)"), ("wav", "WAV")], "m4a")
        self.lbl_combo(b, "Bitrate:", "sp_bitrate",
                       [("", "Automático"), ("320k", "320 kbps"), ("256k", "256 kbps"), ("192k", "192 kbps"),
                        ("128k", "128 kbps"), ("disable", "Sem reencode (copiar)")], "")
        c = self.card(row, "Nome & organização", "left")
        self.lbl_combo(c, "Organização:", "sp_output",
                       [("flat", "Artista - Título (uma pasta)"), ("album", "Álbum / Faixa - Título"),
                        ("full", "Artista / Álbum / Faixa - Título")], "flat")
        self.lbl_check(c, "Gerar playlist .m3u (só playlists)", "sp_m3u")
        d = self.card(row, "Extras", "left")
        self.lbl_combo(d, "Provedor de letras:", "sp_lyrics",
                       [("", "Nenhum"), ("genius", "Genius"), ("musixmatch", "Musixmatch"),
                        ("azlyrics", "AZLyrics"), ("synced", "Synced")], "")
        self.lbl_combo(d, "Se o arquivo já existir:", "sp_overwrite",
                       [("skip", "Pular"), ("force", "Sobrescrever"), ("metadata", "Só atualizar metadados")], "skip")
        self.lbl_entry(d, "Downloads simultâneos (ex: 4):", "sp_threads")
        e = self.card(self.p_sp_dl, "Avançado")
        c = self.cols(e, 2)
        self.lbl_entry(c[0], "Arquivo de cookies (--cookie-file):", "sp_cookie", browse="file")
        self.lbl_entry(c[1], "Args extras do yt-dlp interno:", "sp_ytdlp")

    # ----- parte de baixo: destino, comando, execução, log -----
    def bottom_ui(self, parent):
        r = ttk.Frame(parent)
        r.pack(fill="x")
        ttk.Label(r, text="Pasta de destino (padrão: a pasta do app):").pack(side="left")
        self.var("workdir", str(default_workdir()))
        ttk.Entry(r, textvariable=self.v["workdir"]).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r, text="Escolher…", command=lambda: self.browse("workdir", "dir")).pack(side="left")
        ttk.Button(r, text="Pasta do app",
                   command=lambda: self.v["workdir"].set(str(default_workdir()))).pack(side="left", padx=(4, 0))
        ttk.Button(r, text="Abrir pasta", command=self.open_folder).pack(side="left", padx=(4, 0))

        f = ttk.Frame(parent)
        f.pack(fill="x", pady=(6, 0))
        ttk.Label(f, text="Pasta do FFmpeg (vazio = detectar sozinho):").pack(side="left")
        self.var("ffmpeg_dir", "")
        self.v["ffmpeg_dir"].trace_add("write", lambda *_: self.update_tools_label())
        ttk.Entry(f, textvariable=self.v["ffmpeg_dir"]).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(f, text="Escolher…", command=lambda: self.browse("ffmpeg_dir", "dir")).pack(side="left")

        ttk.Label(parent, text="Comando que será executado:").pack(anchor="w", pady=(8, 0))
        self.preview = tk.Text(parent, height=4, wrap="word", bg="#0f172a", fg="#e2e8f0",
                               font="TkFixedFont", state="disabled", relief="flat", padx=8, pady=6)
        self.preview.pack(fill="x")

        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=8)
        self.btn_run = ttk.Button(bar, text="▶  Executar", style="Run.TButton", command=self.run)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■  Parar", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(bar, text="Copiar comando", command=self.copy_cmd).pack(side="left")
        ttk.Button(bar, text="Limpar log", command=self.clear_log).pack(side="left", padx=6)
        self.status = tk.StringVar(value="Pronto.")
        ttk.Label(bar, textvariable=self.status).pack(side="right")
        self.pb = ttk.Progressbar(bar, length=180, mode="determinate")
        self.pb.pack(side="right", padx=10)

        lf = ttk.Frame(parent)
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, height=10, wrap="word", bg="#111827", fg="#e5e7eb", font="TkFixedFont",
                           state="disabled", relief="flat", padx=8, pady=6)
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, color in (("cmd", "#60a5fa"), ("err", "#f87171"), ("warn", "#fbbf24"), ("ok", "#34d399")):
            self.log.tag_configure(tag, foreground=color)

        self.tools_lbl = ttk.Label(parent, foreground="#6b7280")
        self.tools_lbl.pack(anchor="w", pady=(4, 0))
        self.update_tools_label()

    def update_tools_label(self):
        parts = []
        for t in ("yt-dlp", "instaloader", "spotdl"):
            parts.append(f"{t} {'✓' if resolve_tool(t) else '✗'}")
        d = find_ffmpeg_dir(self.val("ffmpeg_dir")) if "ffmpeg_dir" in self.v else find_ffmpeg_dir()
        if not d:
            parts.insert(1, "ffmpeg ✗ (informe a pasta no campo acima)")
        elif shutil.which("ffmpeg"):
            parts.insert(1, "ffmpeg ✓")
        else:
            parts.insert(1, f"ffmpeg ✓ (fora do PATH: {d})")
        self.tools_lbl.config(text="Ferramentas:  " + "   |   ".join(parts))

    # ---------------- lógica de estado ----------------
    def on_ff_action(self):
        self.sync_ext()
        self.refresh()

    def sync_ext(self):
        act = self.val("ff_action")
        ext = "mp4"
        if act == "gif":
            ext = "gif"
        elif act == "audio":
            m = self.val("ff_ea_mode")
            ext = "m4a" if m in ("copy", "aac") else m
        elif act == "aconv":
            ext = self.val("ff_ac_fmt") or "m4a"
        elif act == "convert":
            ext = self.val("ff_cv_fmt") or "mp4"
        self.v["ff_bout"].set(ext)
        cur = self.val("ff_out")
        base = os.path.splitext(cur)[0] or cur or "saida"
        self.v["ff_out"].set(f"{base}.{ext}")

    def update_visibility(self):
        ym = self.val("yt_mode")
        {"audio": self.pg_audio, "thumb": self.pg_thumb}.get(ym, self.pg_video).tkraise()
        (self.pg_strict_on if self.val("yt_afmt") != "m4a" else self.pg_strict_off).tkraise()
        (self.pg_embsubs_on if ym == "video" else self.pg_embsubs_off).tkraise()
        self.show(self.p_yt_meta, ym == "audio")
        urls = self.yt_text.get("1.0", "end").split()
        in_playlist = any("list=" in u and re.search(r"[?&]v=|youtu\.be/", u) for u in urls)
        self.show(self.yt_pl_note, in_playlist and self.chk("yt_noplaylist") and not self.val("yt_batch"))
        self.show(self.p_ff_single, self.val("ff_mode") == "single")
        self.show(self.p_ff_batch, self.val("ff_mode") == "batch")
        for k, p in self.ff_panels.items():
            self.show(p, k == self.val("ff_action"))
        self.show(self.p_sp_txt, self.val("sp_mode") == "url_txt")
        self.show(self.p_sp_dl, self.val("sp_mode") != "url_txt")

    def refresh(self):
        if not self._ready:
            return
        self.update_visibility()
        self.set_text(self.preview, self.build_job().preview())

    @staticmethod
    def set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    # ---------------- geradores de comando ----------------
    def build_job(self) -> Job:
        i = self.nb.index(self.nb.select())
        return [self.build_ytdlp, self.build_ffmpeg, self.build_instaloader, self.build_spotdl][i]()

    def build_ytdlp(self) -> Job:
        urls = self.yt_text.get("1.0", "end").split()
        batch = self.val("yt_batch")
        folder = self.val("yt_folder")
        mode = self.val("yt_mode")
        missing = "" if (urls or batch) else "Informe um link (ou um arquivo TXT com links)."
        name = self.val("yt_name") or "auto"
        a = []
        if batch and name != "auto":
            a += ["-a", batch]

        if mode == "audio":
            # M4A de saída: prefere a trilha que já é M4A/AAC (só reempacota, sem reconverter)
            if self.chk("yt_strict") or self.val("yt_afmt") == "m4a":
                a += ["-f", "ba[ext=m4a]/ba"]
            a += ["-x", "--audio-format", self.val("yt_afmt"), "--audio-quality", self.val("yt_aq")]
        elif mode == "thumb":
            a += ["--write-thumbnail", "--skip-download"]
        else:
            res = self.val("yt_res")
            h = "" if res == "best" else f"[height<={res}]"
            if self.chk("yt_strict"):
                a += ["-f", f"bv*{h}[ext=mp4]+ba[ext=m4a]/b{h}[ext=mp4]/b{h}"]
            else:
                a += ["-f", f"bv*{h}+ba/b{h}"]
            a += ["--merge-output-format", self.val("yt_cont")]

        if self.chk("yt_noplaylist") and not batch:
            a.append("--no-playlist")
        if self.val("yt_items"):
            a += ["--playlist-items", self.val("yt_items")]
        if self.val("yt_section"):
            a += ["--download-sections", self.val("yt_section")]
        if self.val("yt_rate"):
            a += ["--limit-rate", self.val("yt_rate")]

        pre = a
        prefix = f"{folder}/" if folder else ""
        by_artist = "%(artist,creator,uploader,channel)s - %(title)s.%(ext)s"
        single_tpl = {"auto": by_artist, "artist": by_artist, "title": "%(title)s.%(ext)s",
                      "default": "%(title)s [%(id)s].%(ext)s"}.get(name, by_artist)
        playlist_tpl = "%(playlist_title,playlist)s/%(playlist_index)02d - %(title)s.%(ext)s"

        post = []
        if self.chk("yt_subs"):
            post += ["--write-subs", "--sub-langs", "pt,en"]
        if self.chk("yt_embed_subs") and mode == "video":
            post.append("--embed-subs")
        if self.chk("yt_embed_thumb") and mode != "thumb":
            post.append("--embed-thumbnail")
            if mode == "audio":
                post += ["--convert-thumbnails", "jpg"]
        if self.chk("yt_embed_meta") or mode == "audio":
            post.append("--embed-metadata")

        if mode == "audio":
            for key, field in (("artist", "meta_artist"), ("albumartist", "meta_album_artist"),
                               ("album", "meta_album"), ("title", "meta_title"),
                               ("year", "meta_date"), ("genre", "meta_genre")):
                v = re.sub(r'["%]', "", self.val(f"yt_m_{key}")).replace(":", "\\:")
                if v:
                    post += ["--parse-metadata", f"{v}:%({field})s"]

        cookies = ["--cookies-from-browser", self.val("yt_cookies")] if self.val("yt_cookies") else []
        post += cookies

        if name == "auto":
            auto = dict(pre=pre, post=post, batch=batch,
                        urls=urls or ([] if batch else ["https://www.youtube.com/watch?v=EXEMPLO"]),
                        single_o=prefix + single_tpl, playlist_o=prefix + playlist_tpl,
                        detect=(["--no-playlist"] if "--no-playlist" in pre else []) + cookies)
            return Job("yt-dlp", [], missing=missing, auto=auto)

        a = pre + ([] if (name == "default" and not folder) else ["-o", prefix + single_tpl]) + post
        if not batch:
            a += urls or ["https://www.youtube.com/watch?v=EXEMPLO"]
        return Job("yt-dlp", a, missing=missing)

    def ff_opts(self) -> list:
        act = self.val("ff_action")
        if act == "convert":
            fmt = self.val("ff_cv_fmt")
            if fmt == "mp3":
                return audio_args("mp3", "320k", False)
            if fmt == "m4a":
                return audio_args("m4a", "256k", False)
            if fmt == "flac":
                return audio_args("flac", "", False)
            if fmt == "wav":
                return ["-vn", "-c:a", "pcm_s16le", "-map_metadata", "0"]
            return ["-c:v", self.val("ff_cv_vc"), "-c:a", self.val("ff_cv_ac")]
        if act == "trim":
            r = []
            if self.val("ff_trim_s"):
                r += ["-ss", self.val("ff_trim_s")]
            if self.val("ff_trim_e"):
                r += ["-to", self.val("ff_trim_e")]
            if self.chk("ff_trim_copy"):
                r += ["-c", "copy"]
            return r
        if act == "compress":
            return ["-c:v", self.val("ff_comp_codec") or "libx264", "-crf", self.val("ff_comp_crf") or "23",
                    "-preset", self.val("ff_comp_preset") or "medium", "-c:a", "aac", "-b:a", "128k"]
        if act == "audio":
            mode, br = self.val("ff_ea_mode"), self.val("ff_ea_br")
            if mode == "copy":
                return ["-map", "0:a", "-c:a", "copy", "-map_metadata", "0"]
            if mode == "mp3":
                return audio_args("mp3", br, False)
            if mode == "aac":
                return audio_args("m4a", br, False)
            if mode == "flac":
                return audio_args("flac", "", False)
            return ["-vn"]
        if act == "mute":
            return ["-an", "-c:v", "copy"]
        if act == "resize":
            vf = f"scale={self.val('ff_res_scale') or '1280:720'}"
            if self.val("ff_res_fps"):
                vf += f",fps={self.val('ff_res_fps')}"
            return ["-vf", vf, "-c:a", "copy"]
        if act == "aconv":
            opts = audio_args(self.val("ff_ac_fmt") or "m4a", self.val("ff_ac_br") or "320k", self.chk("ff_ac_cover"))
            for key, tag in (("artist", "artist"), ("albumartist", "album_artist"), ("album", "album"),
                             ("title", "title"), ("year", "date"), ("genre", "genre")):
                v = self.val(f"ff_ac_{key}")
                if v:
                    opts += ["-metadata", f"{tag}={v}"]
            return opts
        if act == "gif":
            w, fps = self.val("ff_gif_w") or "480", self.val("ff_gif_fps") or "15"
            return ["-vf", f"fps={fps},scale={w}:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"]
        return ["-c", "copy"]

    def build_ffmpeg(self) -> Job:
        opts = self.ff_opts()
        ow = "-y" if self.chk("ff_overwrite") else "-n"
        if self.val("ff_mode") == "batch":
            strip = lambda s: re.sub(r"^\*\.?", "", s)
            batch = dict(opts=opts, ow=ow, suffix=self.val("ff_bsuffix"),
                         in_ext=strip(self.val("ff_bin") or "mkv"), out_ext=strip(self.val("ff_bout") or "mp4"))
            return Job("ffmpeg", [], batch=batch)
        src, dst = self.val("ff_in"), self.val("ff_out")
        missing = "" if (src and dst) else "Informe o arquivo de entrada e o de saída."
        return Job("ffmpeg", ["-hide_banner", "-nostdin", ow, "-i", src or "video.mp4", *opts, dst or "saida.mp4"],
                   missing=missing)

    def build_instaloader(self) -> Job:
        target = self.val("in_target")
        missing = "" if target else "Informe o link ou o perfil."
        target = target or "https://www.instagram.com/p/DA_exemplo123/"
        typ, login, pwd = self.val("in_type"), self.val("in_login"), self.val("in_pass")
        a = []
        if login:
            a += ["-l", login]
            if pwd:
                a += ["-p", pwd]
        if self.chk("in_clean"):
            a += ["--no-metadata-json", "--no-captions", "--no-video-thumbnails"]
        if self.val("in_folder"):
            a += ["--dirname-pattern", self.val("in_folder")]

        user = re.sub(r"^https?://(www\.)?instagram\.com/", "", target)
        user = re.sub(r"/.*$", "", user).lstrip("@") or "nome_usuario"
        if typ == "single":
            m = re.search(r"(?:p|reel|tv)/([A-Za-z0-9_-]+)", target)
            code = m.group(1) if m else target.lstrip("@")
            a += ["--", f"-{code}"]
        elif typ == "profile":
            a.append(user)
        elif typ == "stories":
            a += ["--stories", "--no-posts", user]
        else:
            a += ["--highlights", "--no-posts", user]
        return Job("instaloader", a, missing=missing, secrets=(pwd,) if pwd else ())

    def build_spotdl(self) -> Job:
        url = self.val("sp_url")
        missing = "" if url else "Informe o link do Spotify."
        url = url or "https://open.spotify.com/playlist/EXEMPLO"
        if self.val("sp_mode") == "url_txt":
            return Job("spotdl", ["url", url], missing=missing, tee=self.val("sp_txt") or "playlist_links.txt")
        tpl = {"flat": "{artists} - {title}.{output-ext}",
               "album": "{album}/{track-number} - {title}.{output-ext}",
               "full": "{artists}/{album}/{track-number} - {title}.{output-ext}"}[self.val("sp_output") or "flat"]
        a = ["download", url, "--output", tpl, "--format", self.val("sp_format") or "m4a"]
        if self.val("sp_bitrate"):
            a += ["--bitrate", self.val("sp_bitrate")]
        if self.val("sp_overwrite") not in ("", "skip"):
            a += ["--overwrite", self.val("sp_overwrite")]
        if self.val("sp_lyrics"):
            a += ["--lyrics", self.val("sp_lyrics")]
        if self.val("sp_threads"):
            a += ["--threads", self.val("sp_threads")]
        if self.val("sp_cookie"):
            a += ["--cookie-file", self.val("sp_cookie")]
        if self.val("sp_ytdlp"):
            a += ["--yt-dlp-args", self.val("sp_ytdlp")]
        if self.chk("sp_m3u"):
            a += ["--m3u", "{list-name}.m3u8"]
        return Job("spotdl", a, missing=missing)

    # ---------------- execução ----------------
    def workdir(self) -> Path:
        return Path(self.val("workdir") or ".").expanduser()

    def run(self):
        if self.worker and self.worker.is_alive():
            return
        job = self.build_job()
        if job.missing:
            messagebox.showwarning("Faltam dados", job.missing)
            return
        self.ff_dir = find_ffmpeg_dir(self.val("ffmpeg_dir"))
        exe = resolve_tool(job.tool)
        if not exe and job.tool == "ffmpeg" and self.ff_dir:
            exe = [str(Path(self.ff_dir) / ("ffmpeg.exe" if IS_WIN else "ffmpeg"))]
        if not exe:
            messagebox.showerror("Ferramenta não encontrada",
                                 f"'{job.tool}' não foi encontrado.\n\nPara instalar:\n{install_hint(job.tool)}"
                                 + ("\n\nOu use o menu Ferramentas → Baixar/atualizar ferramentas."
                                    if job.tool in ("yt-dlp", "ffmpeg") else ""))
            return
        cwd = self.workdir()
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Pasta de destino inválida", str(e))
            return
        cmds, warns = ([], []) if job.auto else job.expand(cwd)
        if not cmds and not job.auto:
            messagebox.showinfo("Nada para fazer", f"Nenhum arquivo *.{job.batch['in_ext']} em {cwd}.")
            return
        if job.tool in ("yt-dlp", "spotdl") and not self.ff_dir:
            if not messagebox.askyesno(
                    "FFmpeg não encontrado",
                    "O yt-dlp/spotDL precisa do FFmpeg para converter áudio, embutir capa e metadados.\n\n"
                    "Sem ele os arquivos saem incompletos (por exemplo, .m4a sem capa e sem metadados).\n\n"
                    f"Para instalar:  {install_hint('ffmpeg')}\n"
                    "Ou use o menu Ferramentas → Baixar/atualizar ferramentas, "
                    "ou indique a pasta dele no campo \"Pasta do FFmpeg\".\n\nContinuar mesmo assim?",
                    default="no"):
                return
            warns.append("ffmpeg não encontrado — conversão, capa e metadados não serão aplicados.")

        self.stop_flag.clear()
        self.set_running(True)
        self.worker = threading.Thread(target=self._work, args=(job, exe, cmds, warns, cwd), daemon=True)
        self.worker.start()

    def _work(self, job, exe, cmds, warns, cwd):
        put = self.q.put
        failed, tee_fh = 0, None
        for w in warns:
            put(("line", w, "warn", False))
        try:
            if job.tee:
                tee_fh = open(cwd / job.tee, "w", encoding="utf-8")
            tasks = self._auto_urls(job, cwd) if job.auto else cmds
            total = len(tasks)
            if not total:
                put(("line", "Nenhum link encontrado.", "err", False))
                failed += 1
            for i, item in enumerate(tasks, 1):
                if self.stop_flag.is_set():
                    break
                put(("status", f"Executando {i}/{total}…" if total > 1 else "Executando…"))
                args = self._auto_args(job, exe, cwd, item) if job.auto else item
                if self.stop_flag.is_set():
                    break
                put(("line", "$ " + fmt_cmd(job.tool, args, job.secrets), "cmd", False))
                code = self._run_one(exe + args, cwd, tee_fh)
                if code != 0 and not self.stop_flag.is_set():
                    failed += 1
                    put(("line", f"✗ código de saída {code}", "err", False))
        except Exception as e:  # noqa: BLE001
            failed += 1
            put(("line", f"Erro: {e}", "err", False))
        finally:
            if tee_fh:
                tee_fh.close()
        put(("done", failed, self.stop_flag.is_set()))

    # ---- yt-dlp: detecção automática de playlist ----
    @staticmethod
    def _auto_urls(job, cwd) -> list:
        a = job.auto
        if not a["batch"]:
            return list(a["urls"])
        p = Path(a["batch"]).expanduser()
        if not p.is_absolute():
            p = cwd / p
        lines = p.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith(("#", ";", "]"))]

    def _auto_args(self, job, exe, cwd, url) -> list:
        a = job.auto
        title = self._detect_playlist(exe, cwd, url, a["detect"])
        if title:
            self.q.put(("line", f"📂 Playlist detectada: {title}", "ok", False))
            tpl = a["playlist_o"]
        else:
            self.q.put(("line", "Link único (sem playlist) — sem pasta de playlist.", "warn", False))
            tpl = a["single_o"]
        return a["pre"] + ["-o", tpl] + a["post"] + [url]

    def _detect_playlist(self, exe, cwd, url, extra) -> str:
        """Pergunta ao yt-dlp o nome da playlist do link (vazio se não for playlist)."""
        self.q.put(("status", "Verificando se é playlist…"))
        argv = exe + ["--no-warnings", "--flat-playlist", "--playlist-items", "1",
                      "--print", "%(playlist_title,playlist|)s", *extra, url]
        kw = dict(cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                  env=self.child_env(),
                  text=True, encoding="utf-8", errors="replace")
        if IS_WIN:
            kw["creationflags"] = NO_WINDOW
        else:
            kw["start_new_session"] = True
        proc = None
        try:
            proc = subprocess.Popen(argv, **kw)
            self.proc = proc
            out, _ = proc.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            return ""
        except OSError:
            return ""
        finally:
            self.proc = None
        for line in out.splitlines():
            line = line.strip()
            if line and line != "NA":
                return line
        return ""

    def _run_one(self, argv, cwd, tee_fh) -> int:
        kw = dict(cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, bufsize=0, env=self.child_env(),
                  stderr=subprocess.PIPE if tee_fh else subprocess.STDOUT)
        if IS_WIN:
            kw["creationflags"] = NO_WINDOW
        else:
            kw["start_new_session"] = True
        proc = subprocess.Popen(argv, **kw)
        self.proc = proc
        threads = [threading.Thread(target=self._pump, args=(proc.stdout, tee_fh), daemon=True)]
        if tee_fh:
            threads.append(threading.Thread(target=self._pump, args=(proc.stderr, None), daemon=True))
        for t in threads:
            t.start()
        proc.wait()
        for t in threads:
            t.join(timeout=2)
        self.proc = None
        return proc.returncode

    def _pump(self, stream, tee_fh):
        """Lê a saída em tempo real; \\r = linha de progresso (substitui a anterior)."""
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""

        def emit(line, progress):
            if not line.strip():
                return
            tag = "err" if re.match(r"(ERROR|Error)", line) else "warn" if line.startswith("WARNING") else None
            self.q.put(("line", line, tag, progress))
            m = re.search(r"(\d{1,3}(?:\.\d+)?)%", line)
            if m:
                self.q.put(("pct", min(float(m.group(1)), 100.0)))
            if tee_fh and not progress:
                tee_fh.write(line + "\n")
                tee_fh.flush()

        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            buf += dec.decode(chunk)
            while True:
                m = re.search(r"\r\n|\r|\n", buf)
                if not m:
                    break
                line, sep = buf[:m.start()], m.group()
                buf = buf[m.end():]
                emit(line, sep == "\r")
        emit(buf + dec.decode(b"", final=True), False)

    def child_env(self) -> dict:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        if getattr(sys, "frozen", False):  # PyInstaller altera LD_LIBRARY_PATH; restaura o original
            if "LD_LIBRARY_PATH_ORIG" in env:
                env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
            else:
                env.pop("LD_LIBRARY_PATH", None)
        d = getattr(self, "ff_dir", "")
        parts = ([d] if d else []) + [env.get("PATH", "")] + [str(x) for x in extra_bin_dirs()]
        env["PATH"] = os.pathsep.join(x for x in parts if x)
        return env

    def stop(self):
        self.stop_flag.set()
        p = self.proc
        if p and p.poll() is None:
            kill_tree(p)

    # ---------------- UI de execução ----------------
    def set_running(self, running: bool):
        self.btn_run.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        if running:
            self.pb.configure(mode="indeterminate", value=0)
            self.pb.start(12)
        self.status.set("Executando…" if running else self.status.get())

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "line":
                    self.log_line(m[1], m[2], m[3])
                elif m[0] == "pct":
                    self.pb.stop()
                    self.pb.configure(mode="determinate", value=m[1])
                elif m[0] == "status":
                    self.status.set(m[1])
                elif m[0] == "done":
                    self.finish(m[1], m[2])
        except queue.Empty:
            pass
        self.after(60, self.poll)

    def finish(self, failed, stopped):
        self.pb.stop()
        if stopped:
            msg, tag, pct = "■ Interrompido.", "warn", 0
        elif failed:
            msg, tag, pct = f"✗ Terminou com {failed} erro(s).", "err", 0
        else:
            msg, tag, pct = "✓ Concluído!", "ok", 100
        self.pb.configure(mode="determinate", value=pct)
        self.log_line(msg, tag, False)
        self.status.set(msg)
        self.set_running(False)
        self.status.set(msg)
        self.update_tools_label()

    def log_line(self, text, tag=None, progress=False):
        w = self.log
        w.configure(state="normal")
        if self._last_progress:
            w.delete("end-2l linestart", "end-1c")
        w.insert("end", text + "\n", tag or ())
        w.see("end")
        w.configure(state="disabled")
        self._last_progress = progress

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._last_progress = False

    def copy_cmd(self):
        job = self.build_job()
        if job.batch:
            cmds, _ = job.expand(self.workdir())
            text = fmt_cmd(job.tool, cmds[0]) if cmds else job.preview().splitlines()[0]
        else:
            text = job.copy_text()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.set("Comando copiado!")

    def start_setup(self, all_platforms: bool):
        if self.worker and self.worker.is_alive():
            return
        plats = list(SHARED_PLATFORMS) if all_platforms else [plat_key()]
        if all_platforms and plat_key() in TOOL_SOURCES and plat_key() not in plats:
            plats.append(plat_key())
        if not all(p in TOOL_SOURCES for p in plats):
            messagebox.showinfo("Ferramentas", f"Sem download automático para {plat_key()}.\n\n"
                                               f"{install_hint('yt-dlp')}\n{install_hint('ffmpeg')}")
            return
        self.stop_flag.clear()
        self.set_running(True)
        self.log_line("Baixando ferramentas para: " + ", ".join(plats), "cmd", False)
        self.worker = threading.Thread(target=self._setup_work, args=(plats,), daemon=True)
        self.worker.start()

    def _setup_work(self, plats):
        put = self.q.put

        def log(text, tag=None, progress=False):
            put(("line", text, tag, progress))

        def prog(pct, text):
            put(("pct", pct))
            put(("status", f"Baixando… {text}"))

        failed = 0
        try:
            for pl in plats:
                if self.stop_flag.is_set():
                    break
                try:
                    setup_platform(pl, log, prog, self.stop_flag)
                    log(f"✓ {pl}: {portable_src(pl)}", "ok")
                except Cancelled:
                    break
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    log(f"✗ {pl}: {e}", "err")
        finally:
            reset_portable_cache()
        put(("done", failed, self.stop_flag.is_set()))

    def open_folder(self, p=None):
        p = Path(p) if p else self.workdir()
        p.mkdir(parents=True, exist_ok=True)
        try:
            if IS_WIN:
                os.startfile(p)  # type: ignore[attr-defined]
            elif IS_MAC:
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            messagebox.showinfo("Abrir pasta", f"Não consegui abrir o gerenciador de arquivos ({e}).\n\nPasta:\n{p}")


def q_exec(text) -> str:
    """Quoting de argumentos do campo Exec= de um arquivo .desktop."""
    t = str(text).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + t.replace("%", "%%") + '"'


def install_launcher() -> int:
    """Linux: cria o atalho do app no menu de aplicativos (~/.local/share/applications)."""
    if IS_WIN or IS_MAC:
        print("O atalho de menu (.desktop) é só para Linux.")
        return 1
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        exec_line = q_exec(appimage)
    elif getattr(sys, "frozen", False):
        print("Rode este comando pelo arquivo .AppImage (ele mesmo).")
        return 1
    else:
        script = Path(__file__).resolve()
        try:
            script.chmod(script.stat().st_mode | 0o111)
        except OSError:
            pass
        exec_line = f"{q_exec(sys.executable)} {q_exec(script)}"
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    apps = data / "applications"
    apps.mkdir(parents=True, exist_ok=True)

    target = apps / "cli-command-studio.desktop"
    target.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=CLI Command Studio\n"
        "Comment=yt-dlp, FFmpeg, Instaloader e spotDL numa interface só\n"
        f"Exec={exec_line}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Utility;\n"
        "Icon=applications-multimedia\n"
        "StartupWMClass=CliCommandStudio\n",
        encoding="utf-8")
    print(f"Atalho criado: {target}\nProcure por 'CLI Command Studio' no menu de aplicativos.")
    return 0


def cli_setup_tools() -> int:
    plats = list(SHARED_PLATFORMS) if "--all" in sys.argv else [plat_key()]

    def log(text, tag=None, progress=False):
        print(("\r" if progress else "") + text, end="" if progress else "\n", flush=True)

    failed = 0
    for pl in plats:
        try:
            setup_platform(pl, log, lambda *_: None, threading.Event())
            print(f"\n✓ {pl}: {portable_src(pl)}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"\n✗ {pl}: {e}")
    return 1 if failed else 0


def main():
    if "--setup-tools" in sys.argv:
        sys.exit(cli_setup_tools())
    if "--install-launcher" in sys.argv:
        sys.exit(install_launcher())
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
