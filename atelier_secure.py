"""
Atelier PDM - vault encryption (atelier_secure.py)
==================================================

Optional encryption of the files stored in the vault, for vaults kept on a cloud drive (Google Drive,
OneDrive, Dropbox) or a network server / NAS. Your local working folder (sandbox) is never encrypted, so
SolidCraft, PhysCAD and FreeCAD open files exactly as before.

How it works
  * Each vault has one random 256-bit data key, stored in 'atelier_vault.key' in the vault, wrapped twice:
    by the vault passphrase and by a recovery code (scrypt + AES-256-GCM). Lose both and the files are gone.
  * Vault files are encrypted with AES-256-GCM in 1 MiB chunks (tamper and truncation are detected).
  * Each PC asks for the passphrase once; the key is then kept for this Windows account only (Windows DPAPI).
  * Encryption happens when files go into the vault (check-in, add files, new meshes); decryption when they
    come out (check-out, previews, exports). Encrypted and unencrypted files can sit side by side, so files
    dropped into the vault by hand still work and are encrypted by "Encrypt waiting files".
  * With encryption on, Export STEP & PDF writes to <working folder>\\Manufacturing_Output so the files you
    send to suppliers are readable.
  * File and folder names, part numbers and the database stay readable - only file contents are encrypted.

Installed into atelier_pdm.py by:   atelier_secure.install(sys.modules[__name__])
Needs the 'cryptography' package (bundled in the installed program; for scripts: pip install cryptography).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil as _real_shutil
import sys
import tempfile
import time
import types

SECURE_VERSION = "1.0"
KEY_FILE = "atelier_vault.key"
MAGIC = b"ATLENC1\n"
CHUNK = 1 << 20
HEADER_LEN = len(MAGIC) + 8 + 8 + 4
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSPHRASE = 10
TEMP_SUFFIXES = (".partial", ".enc-tmp", ".dec-tmp")

E = None                      # the atelier_pdm module, set by install()
_KEY = {"dk": None, "id": None}
_THUMBS = {}
_ORIG = {}


class VaultLocked(OSError):
    """The vault is encrypted and this program doesn't have its key."""


# =============================================================================
# Crypto primitives
# =============================================================================
def _aes():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise OSError("Vault encryption needs the 'cryptography' package. Install it with:  "
                      "pip install cryptography") from None
    return AESGCM


def crypto_available():
    try:
        _aes()
        return True
    except OSError:
        return False


def key_id_of(dk):
    return hashlib.sha256(b"atelier-key-id" + dk).digest()[:8]


def _kek(secret, salt, n, r, p):
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)


def normalise_recovery(code):
    c = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return c.translate(str.maketrans({"0": "O", "1": "I", "8": "B"}))


def new_recovery_code():
    raw = base64.b32encode(secrets.token_bytes(20)).decode("ascii")      # 32 chars
    return "-".join(raw[i:i + 4] for i in range(0, 32, 4))


def _wrap(dk, secret, kind):
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    kek = _kek(secret, salt, **SCRYPT)
    ct = _aes()(kek).encrypt(nonce, dk, b"atelier-vault-key-v1|" + key_id_of(dk))
    b = lambda v: base64.b64encode(v).decode("ascii")
    return {"type": kind, "kdf": "scrypt", **SCRYPT, "salt": b(salt), "nonce": b(nonce), "wrapped": b(ct)}


def _unwrap(slot, secret, key_id):
    from cryptography.exceptions import InvalidTag
    d = lambda k: base64.b64decode(slot[k])
    kek = _kek(secret, d("salt"), int(slot["n"]), int(slot["r"]), int(slot["p"]))
    try:
        return _aes()(kek).decrypt(d("nonce"), d("wrapped"), b"atelier-vault-key-v1|" + key_id)
    except InvalidTag:
        return None


def make_key_file(passphrase, recovery_code, created_by=""):
    dk = secrets.token_bytes(32)
    data = {"format": "atelier-vault-key", "version": 1, "key_id": key_id_of(dk).hex(),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "created_by": created_by,
            "slots": [_wrap(dk, passphrase, "passphrase"), _wrap(dk, normalise_recovery(recovery_code), "recovery")]}
    return dk, data


def open_key_file(data, secret):
    """Data key from the passphrase or the recovery code, or None."""
    kid = bytes.fromhex(data["key_id"])
    for slot in data.get("slots", []):
        s = normalise_recovery(secret) if slot.get("type") == "recovery" else secret
        dk = _unwrap(slot, s, kid)
        if dk is not None and hmac.compare_digest(key_id_of(dk), kid):
            return dk
    return None


# ---- streaming file format --------------------------------------------------------------------------------
def is_encrypted(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def encrypt_stream(fin, fout, dk):
    aes = _aes()(dk)
    prefix = secrets.token_bytes(8)
    header = MAGIC + key_id_of(dk) + prefix + CHUNK.to_bytes(4, "big")
    fout.write(header)
    i, chunk = 0, fin.read(CHUNK)
    while True:
        nxt = fin.read(CHUNK) if len(chunk) == CHUNK else b""
        final = not nxt
        aad = header + i.to_bytes(4, "big") + (b"\x01" if final else b"\x00")
        ct = aes.encrypt(prefix + i.to_bytes(4, "big"), chunk, aad)
        fout.write(len(ct).to_bytes(4, "big") + ct)
        if final:
            return
        chunk, i = nxt, i + 1


def decrypt_stream(fin, dk, sink):
    """Feeds plaintext chunks to sink(bytes). Raises ValueError on damage or the wrong key."""
    from cryptography.exceptions import InvalidTag
    header = fin.read(HEADER_LEN)
    if len(header) < HEADER_LEN or header[:len(MAGIC)] != MAGIC:
        raise ValueError("not an Atelier encrypted file")
    if not hmac.compare_digest(header[len(MAGIC):len(MAGIC) + 8], key_id_of(dk)):
        raise ValueError("this file was encrypted with a different vault key")
    prefix = header[len(MAGIC) + 8:len(MAGIC) + 16]
    aes, i = _aes()(dk), 0
    n = fin.read(4)
    while True:
        if len(n) < 4:
            raise ValueError("the encrypted file is incomplete")
        ct = fin.read(int.from_bytes(n, "big"))
        n = fin.read(4)
        final = not n
        aad = header + i.to_bytes(4, "big") + (b"\x01" if final else b"\x00")
        try:
            sink(aes.decrypt(prefix + i.to_bytes(4, "big"), ct, aad))
        except InvalidTag:
            raise ValueError("the encrypted file is damaged or incomplete") from None
        if final:
            return
        i += 1


def _atomic(dst, write):
    tmp = dst + ".enc-tmp"
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    try:
        with open(tmp, "wb") as fout:
            write(fout)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def encrypt_file(src, dst, dk):
    with open(src, "rb") as fin:
        _atomic(dst, lambda fout: encrypt_stream(fin, fout, dk))


def decrypt_file(src, dst, dk):
    with open(src, "rb") as fin:
        try:
            _atomic(dst, lambda fout: decrypt_stream(fin, dk, fout.write))
        except ValueError as e:
            raise OSError(f"Can't decrypt '{os.path.basename(src)}': {e}") from None


def decrypt_bytes(path, dk):
    out = []
    with open(path, "rb") as fin:
        decrypt_stream(fin, dk, out.append)
    return b"".join(out)


# =============================================================================
# Per-PC key cache (Windows DPAPI: only this Windows account on this PC can read it)
# =============================================================================
def _dpapi(data, protect):
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        c32, k32 = ctypes.WinDLL("crypt32"), ctypes.WinDLL("kernel32")
        buf = ctypes.create_string_buffer(data, len(data))
        bin_, bout = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
        entropy = b"Atelier PDM vault key"
        ebuf = ctypes.create_string_buffer(entropy, len(entropy))
        ent = BLOB(len(entropy), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
        fn = c32.CryptProtectData if protect else c32.CryptUnprotectData
        args = (ctypes.byref(bin_), "Atelier PDM" if protect else None, ctypes.byref(ent), None, None, 0x1,
                ctypes.byref(bout))
        if not fn(*args):
            return None
        try:
            return ctypes.string_at(bout.pbData, bout.cbData)
        finally:
            k32.LocalFree(bout.pbData)
    except (OSError, AttributeError, ValueError):
        return None


def _cache_key(dk):
    blob = _dpapi(dk, True)
    if not blob or E is None:
        return
    s = E.load_settings()
    store = dict(s.get("vault_keys") or {})
    store[key_id_of(dk).hex()] = base64.b64encode(blob).decode("ascii")
    s["vault_keys"] = store
    E.save_settings(s)


def _cached_key(key_id_hex):
    if E is None:
        return None
    blob = (E.load_settings().get("vault_keys") or {}).get(key_id_hex)
    if not blob:
        return None
    dk = _dpapi(base64.b64decode(blob), False)
    return dk if dk and key_id_of(dk).hex() == key_id_hex else None


def forget_cached_key():
    if E is None:
        return
    s = E.load_settings()
    if s.pop("vault_keys", None) is not None:
        E.save_settings(s)


# =============================================================================
# Vault state
# =============================================================================
def key_path():
    return os.path.join(E.VAULT_DIR, KEY_FILE)


def read_key_file():
    try:
        with open(key_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if data.get("format") == "atelier-vault-key" else None
    except (OSError, ValueError):
        return None


def _write_key_file(data):
    _atomic(key_path(), lambda f: f.write(json.dumps(data, indent=2).encode("utf-8")))


def is_enabled():
    return E is not None and os.path.exists(key_path())


def is_unlocked():
    data = read_key_file()
    return bool(data and _KEY["dk"] and _KEY["id"] == data["key_id"])


def _set_key(dk):
    _KEY["dk"], _KEY["id"] = dk, key_id_of(dk).hex()
    _relocate_output()


def _need_key():
    if not is_unlocked():
        raise VaultLocked("The vault is encrypted and isn't unlocked on this PC. Restart Atelier PDM and enter "
                          "the vault passphrase.")
    return _KEY["dk"]


def _local_output_dir():
    return os.path.join(E.LOCAL_WORKSPACE, "Manufacturing_Output")


def _relocate_output():
    if E is None:
        return
    _ORIG.setdefault("OUTPUT_DIR", E.OUTPUT_DIR)
    E.OUTPUT_DIR = _local_output_dir() if is_enabled() else _ORIG["OUTPUT_DIR"]


def unlock(secret, remember=True):
    data = read_key_file()
    if not data:
        return True
    dk = open_key_file(data, secret)
    if dk is None:
        return False
    _set_key(dk)
    if remember:
        _cache_key(dk)
    return True


def try_cached_unlock():
    data = read_key_file()
    if not data:
        _relocate_output()
        return True
    if is_unlocked():
        return True
    dk = _cached_key(data["key_id"])
    if dk:
        _set_key(dk)
        return True
    return False


# =============================================================================
# Which files are vault data, and the vault-aware copy
# =============================================================================
def _managed_exts():
    return {e.lower() for e in set(getattr(E, "VAULT_EXTS", ())) | set(getattr(E, "DERIVED_EXTS", ()))}


def _clean_name(path):
    p = path
    for suf in TEMP_SUFFIXES:
        if p.lower().endswith(suf):
            p = p[:-len(suf)]
    return p


def is_managed(path):
    return os.path.splitext(_clean_name(path))[1].lower() in _managed_exts()


def in_vault(path):
    if E is None:
        return False
    try:
        if E.is_under(path, E.LOCAL_WORKSPACE):
            return False
        return E.is_under(path, E.VAULT_DIR)
    except (ValueError, OSError):
        return False


def vault_copy(src, dst, *args, **kwargs):
    """shutil.copy2 that encrypts files going into the vault and decrypts files coming out of it."""
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src))
    s_in, d_in = in_vault(src), in_vault(dst)
    if s_in and not d_in and is_encrypted(src):
        decrypt_file(src, dst, _need_key())
        _real_shutil.copystat(src, dst)
        return dst
    if d_in and not s_in and is_managed(dst) and not is_encrypted(src):
        if is_enabled():
            encrypt_file(src, dst, _need_key())
            _real_shutil.copystat(src, dst)
            return dst
    return _real_shutil.copy2(src, dst, *args, **kwargs)


class _ShutilProxy(types.ModuleType):
    """Stands in for the 'shutil' module inside atelier_pdm / atelier_panel; only copy2/copy differ."""

    def __init__(self):
        super().__init__("shutil")

    def __getattr__(self, name):
        return getattr(_real_shutil, name)

    @staticmethod
    def copy2(src, dst, *args, **kwargs):
        return vault_copy(src, dst, *args, **kwargs)

    copy = copy2


secure_shutil = _ShutilProxy()


class plain_copy:
    """Context manager: a readable path for a vault file (a temporary decrypted copy if it's encrypted)."""

    def __init__(self, path):
        self.path, self.tmpdir = path, None

    def __enter__(self):
        if not self.path or not is_encrypted(self.path):
            return self.path
        base = os.path.join(E.LOCAL_WORKSPACE, "_tmp")
        os.makedirs(base, exist_ok=True)
        self.tmpdir = tempfile.mkdtemp(prefix="plain_", dir=base)
        out = os.path.join(self.tmpdir, os.path.basename(self.path))
        decrypt_file(self.path, out, _need_key())
        _real_shutil.copystat(self.path, out)
        return out

    def __exit__(self, *exc):
        if self.tmpdir:
            _real_shutil.rmtree(self.tmpdir, ignore_errors=True)
        return False


# =============================================================================
# Converting a whole vault
# =============================================================================
def vault_files_to_convert(encrypt):
    """Vault data files that still need encrypting (or decrypting)."""
    out = []
    for root, dirs, names in os.walk(E.VAULT_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__",)]
        if E.is_under(root, E.LOCAL_WORKSPACE):
            continue
        for n in names:
            p = os.path.join(root, n)
            if n.lower().endswith(TEMP_SUFFIXES) or not is_managed(p):
                continue
            if is_encrypted(p) != encrypt:
                out.append(p)
    return out


def convert_files(paths, encrypt, progress=None):
    """Encrypts/decrypts in place, keeping modification times. Returns (done, [(path, error)])."""
    dk = _need_key()
    done, failed = 0, []
    for i, p in enumerate(paths):
        try:
            st = os.stat(p)
            (encrypt_file if encrypt else decrypt_file)(p, p, dk)
            os.utime(p, (st.st_atime, st.st_mtime))
            done += 1
        except (OSError, ValueError) as e:
            failed.append((p, str(e)))
        if progress:
            progress(i + 1, len(paths), p)
    return done, failed


def status():
    """(enabled, unlocked, encrypted_count, waiting_count)."""
    if not is_enabled():
        return False, False, 0, 0
    enc = waiting = 0
    for root, dirs, names in os.walk(E.VAULT_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            p = os.path.join(root, n)
            if n.lower().endswith(TEMP_SUFFIXES) or not is_managed(p):
                continue
            if is_encrypted(p):
                enc += 1
            else:
                waiting += 1
    return True, is_unlocked(), enc, waiting


def enable(passphrase, recovery_code, by=""):
    """Creates the vault key (doesn't convert existing files - see convert_files)."""
    if is_enabled():
        raise OSError("Encryption is already on for this vault.")
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"The passphrase must be at least {MIN_PASSPHRASE} characters.")
    dk, data = make_key_file(passphrase, recovery_code, by)
    _write_key_file(data)
    _set_key(dk)
    _cache_key(dk)
    return dk


def disable():
    """Removes the key file once nothing is left encrypted. The old key file is kept in _History."""
    if not is_enabled():
        return
    left = vault_files_to_convert(encrypt=False)
    if left:
        raise OSError(f"{len(left)} file(s) are still encrypted. Decrypt them first.")
    os.makedirs(E.HISTORY_DIR, exist_ok=True)
    _real_shutil.move(key_path(), os.path.join(E.HISTORY_DIR, f"{KEY_FILE}.{time.strftime('%Y%m%d_%H%M%S')}.old"))
    _KEY["dk"] = _KEY["id"] = None
    forget_cached_key()
    _relocate_output()


def change_passphrase(new_passphrase, new_recovery_code=None):
    dk = _need_key()
    if len(new_passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"The passphrase must be at least {MIN_PASSPHRASE} characters.")
    data = read_key_file()
    slots = [_wrap(dk, new_passphrase, "passphrase")]
    if new_recovery_code:
        slots.append(_wrap(dk, normalise_recovery(new_recovery_code), "recovery"))
    else:
        slots += [s for s in data["slots"] if s.get("type") == "recovery"]
    data["slots"] = slots
    _write_key_file(data)


# =============================================================================
# Wrapping atelier_pdm
# =============================================================================
def _wrap_reader(name, cache=False, locked=None):
    orig = getattr(E, name, None)
    if orig is None or getattr(orig, "_atelier_secure", False):
        return
    _ORIG[name] = orig

    def wrapper(path, *a, **k):
        if not path or not is_encrypted(path):
            return orig(path, *a, **k)
        if cache:
            try:
                key = (os.path.abspath(path), os.path.getmtime(path), name)
            except OSError:
                key = None
            if key in _THUMBS:
                return _THUMBS[key]
        try:
            with plain_copy(path) as p:
                res = orig(p, *a, **k)
        except (VaultLocked, OSError):
            return locked() if callable(locked) else locked
        if cache and key:
            if len(_THUMBS) > 200:
                _THUMBS.clear()
            _THUMBS[key] = res
        return res
    wrapper._atelier_secure = True
    wrapper.__name__, wrapper.__doc__ = orig.__name__, orig.__doc__
    setattr(E, name, wrapper)


def _wrap_hash():
    orig = getattr(E, "get_file_hash", None)
    if orig is None or getattr(orig, "_atelier_secure", False):
        return

    def get_file_hash(filepath):
        if not filepath or not is_encrypted(filepath):
            return orig(filepath)
        h = hashlib.sha256()
        with open(filepath, "rb") as fin:
            decrypt_stream(fin, _need_key(), h.update)
        return h.hexdigest()
    get_file_hash._atelier_secure = True
    E.get_file_hash = get_file_hash


def _wrap_convert():
    orig = getattr(E, "cad_convert", None)
    if orig is None or getattr(orig, "_atelier_secure", False):
        return

    def cad_convert(src, out):
        try:
            with plain_copy(src) as p:
                return orig(p, out)
        except (VaultLocked, OSError) as e:
            return False, str(e)
    cad_convert._atelier_secure = True
    E.cad_convert = cad_convert

    gen = getattr(E, "generate_stl_companion", None)
    if gen is None:
        return

    def generate_stl_companion(path):
        stl, msg = gen(path)
        if stl and is_enabled() and in_vault(stl) and is_managed(stl) and not is_encrypted(stl):
            try:
                encrypt_file(stl, stl, _need_key())
            except OSError as e:
                return None, str(e)
        return stl, msg
    generate_stl_companion._atelier_secure = True
    E.generate_stl_companion = generate_stl_companion


def _wrap_sign_in():
    orig = getattr(E, "sign_in", None)
    if orig is None or getattr(orig, "_atelier_secure", False):
        return

    def sign_in(ctx, switching=False):
        ok = orig(ctx, switching)
        if not ok or switching:
            return ok
        return unlock_interactive(ctx)
    sign_in._atelier_secure = True
    E.sign_in = sign_in


def unlock_interactive(ctx, parent=None):
    """Asks for the passphrase (or recovery code) if this PC doesn't have the key yet. True when usable."""
    if try_cached_unlock():
        return True
    if not crypto_available():
        from tkinter import messagebox
        messagebox.showerror(E.APP_NAME, "This vault is encrypted, but the encryption library is missing.\n\n"
                             "Install it with:  pip install cryptography", parent=parent)
        return False

    def submit(v):
        secret = v["pw"]
        if not secret:
            return "error", "Enter the vault passphrase or the recovery code."
        if unlock(secret, remember=True):
            return True
        return "error", "That passphrase or recovery code doesn't open this vault."
    res = E.run_form(ctx, f"{E.APP_NAME} · Unlock vault", "Unlock the vault",
                     "This vault is encrypted. Enter the vault passphrase - or the recovery code. "
                     "It's remembered for your Windows account on this PC.",
                     [("pw", "Vault passphrase or recovery code", True, "")], submit, "Unlock", "Exit",
                     width=400, parent=parent)
    return bool(res)


def install(module):
    """Hooks encryption into atelier_pdm. Safe to call more than once."""
    global E
    E = module
    if getattr(module, "_secure_installed", False):
        return
    module._secure_installed = True
    module.secure_shutil = secure_shutil
    module.vault_copy = vault_copy
    module.shutil = secure_shutil
    _wrap_hash()
    _wrap_reader("get_dependencies", locked=list)
    _wrap_reader("parse_stl_file", locked=lambda: ([], []))
    for name in ("fcstd_thumbnail", "physcad_thumbnail"):
        _wrap_reader(name, cache=True)
    _wrap_convert()
    _wrap_sign_in()
    _relocate_output()


# =============================================================================
# Administration > Security section
# =============================================================================
def admin_section(app, parent, dlg, S, run, regrab):
    import tkinter as tk
    from tkinter import ttk, messagebox

    box = ttk.Frame(parent, style="Dialog.TFrame")
    box.pack(fill="x", anchor="w", pady=(S(18), 0))
    ttk.Separator(box, orient="horizontal").pack(fill="x", pady=(0, S(12)))
    var = tk.BooleanVar(value=is_enabled())
    chk = ttk.Checkbutton(box, text="Encrypt files stored in the vault (recommended for cloud drives and servers)",
                          variable=var, style="Dialog.TCheckbutton")
    chk.pack(anchor="w")
    info = ttk.Label(box, text="", style="CardMuted.TLabel", wraplength=S(820), justify="left")
    info.pack(anchor="w", pady=(S(2), S(6)))
    row = ttk.Frame(box, style="Dialog.TFrame")
    row.pack(anchor="w")
    b_wait = ttk.Button(row, text="Encrypt waiting files")
    b_pass = ttk.Button(row, text="Change passphrase…")
    b_code = ttk.Button(row, text="New recovery code…")

    def refresh():
        var.set(is_enabled())
        if not crypto_available():
            info.configure(text="Not available: the 'cryptography' package is missing (pip install cryptography).")
            chk.state(["disabled"])
            return
        on, unlocked, enc, waiting = status()
        if not on:
            where = ("The vault is on a cloud or sync folder - encryption is recommended."
                     if E.db_location_warning() else "Your working folder on this PC is never encrypted.")
            info.configure(text="Off. File contents in the vault are stored as normal files. " + where)
        else:
            info.configure(text=f"On · {enc} file(s) encrypted" + (f", {waiting} waiting" if waiting else "") +
                           ". File names, folders, part numbers and the database stay readable. Exports go to "
                           f"{_local_output_dir()}." + ("" if unlocked else "  (Locked on this PC.)"))
        for b, show in ((b_wait, on and waiting and unlocked), (b_pass, on and unlocked), (b_code, on and unlocked)):
            if show:
                b.pack(side="left", padx=(0, S(8)))
            else:
                b.pack_forget()

    def progress_run(paths, encrypt, title):
        if not paths:
            return 0, []
        top = tk.Toplevel(dlg)
        top.title(title)
        top.transient(dlg)
        top.configure(bg=app.t["panel_bg"])
        frm = ttk.Frame(top, style="Dialog.TFrame", padding=S(16))
        frm.pack(fill="both", expand=True)
        lbl = ttk.Label(frm, text="", style="CardMuted.TLabel", width=60)
        lbl.pack(anchor="w")
        bar = ttk.Progressbar(frm, maximum=len(paths), length=S(420))
        bar.pack(fill="x", pady=(S(8), 0))
        top.grab_set()

        def cb(i, n, p):
            bar["value"] = i
            lbl.configure(text=f"{i} of {n}:  {os.path.basename(p)}")
            top.update()
        try:
            return convert_files(paths, encrypt, cb)
        finally:
            top.destroy()
            regrab()

    def report(done, failed, verb):
        msg = f"{verb} {done} file(s)."
        if failed:
            msg += f" {len(failed)} couldn't be done (first: {os.path.basename(failed[0][0])}: {failed[0][1]})."
        run(("warn" if failed else "ok", msg), lambda: None)
        E.log_audit("[settings]", f"Vault {verb.lower()}", "", msg)

    def ask_recovery_saved(code):
        top = E.ThemedDialog(app, "Recovery code", "Save your recovery code",
                             "If the passphrase is forgotten, this code is the only other way to open the vault. "
                             "Store it somewhere safe and separate from this PC (a password manager or printed).",
                             width=460, parent=dlg)
        e = ttk.Entry(top.body, font=("Consolas", 12))
        e.insert(0, code)
        e.state(["readonly"])
        e.pack(fill="x", ipady=S(4))
        row2 = ttk.Frame(top.body, style="Dialog.TFrame")
        row2.pack(fill="x", pady=(S(8), 0))

        def copy():
            top.clipboard_clear()
            top.clipboard_append(code)

        def save():
            from tkinter import filedialog
            p = filedialog.asksaveasfilename(parent=top, title="Save recovery code", defaultextension=".txt",
                                             initialfile="Atelier PDM recovery code.txt")
            if p:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(f"Atelier PDM vault recovery code\nVault: {E.VAULT_DIR}\nCode:  {code}\n")
        ttk.Button(row2, text="Copy", command=copy).pack(side="left")
        ttk.Button(row2, text="Save to file…", command=save).pack(side="left", padx=(S(8), 0))
        ok = tk.BooleanVar(value=False)
        ttk.Checkbutton(top.body, text="I've stored the recovery code somewhere safe", variable=ok,
                        style="Dialog.TCheckbutton").pack(anchor="w", pady=(S(10), 0))
        err = ttk.Label(top.body, text="", style="CardDanger.TLabel")
        err.pack(anchor="w")

        def validate():
            if not ok.get():
                err.configure(text="Tick the box once the code is stored.")
                return E.INVALID
            return True
        top.validate = validate
        top.add_buttons("Continue", "Accent.TButton", None)
        res = top.show()
        regrab()
        return bool(res)

    def ask_passphrase(title, heading):
        def submit(v):
            if len(v["p1"]) < MIN_PASSPHRASE:
                return "error", f"Use at least {MIN_PASSPHRASE} characters - a short sentence works well."
            if v["p1"] != v["p2"]:
                return "error", "The passphrases don't match."
            return v["p1"]
        res = E.run_form(app, title, heading,
                         "Everyone using this vault will need it once on each PC. It can't be recovered without "
                         "the recovery code.",
                         [("p1", f"Vault passphrase (at least {MIN_PASSPHRASE} characters)", True, ""),
                          ("p2", "Confirm passphrase", True, "")], submit, "Continue", width=420, parent=dlg)
        regrab()
        return res

    def toggle():
        want = var.get()
        var.set(is_enabled())
        if want == is_enabled():
            return
        try:
            if want:
                if not messagebox.askyesno("Encrypt the vault", "Turn on encryption for this vault?\n\n"
                                           "• Everyone using it will need the passphrase once on each PC.\n"
                                           "• Older copies of Atelier PDM (the .py scripts and .cmd launchers) "
                                           "can't open encrypted files - only use the installed program.\n"
                                           "• Existing files are encrypted now; this can take a while.",
                                           parent=dlg):
                    return
                pw = ask_passphrase("Vault passphrase", "Choose a vault passphrase")
                if not pw:
                    return
                code = new_recovery_code()
                if not ask_recovery_saved(code):
                    return
                enable(pw, code, by=E.USER_NAME)
                E.log_audit("[settings]", "Encryption On", "", f"by {E.USER_NAME}")
                done, failed = progress_run(vault_files_to_convert(True), True, "Encrypting the vault")
                report(done, failed, "Encrypted")
            else:
                if not is_unlocked():
                    run(("error", "Unlock the vault on this PC first (restart Atelier PDM)."), lambda: None)
                    return
                if not messagebox.askyesno("Decrypt the vault", "Turn encryption off and decrypt every file in "
                                           "the vault?\n\nAnyone with access to the vault folder will be able to "
                                           "read the files.", icon="warning", parent=dlg):
                    return
                done, failed = progress_run(vault_files_to_convert(False), False, "Decrypting the vault")
                if not failed:
                    disable()
                    E.log_audit("[settings]", "Encryption Off", "", f"by {E.USER_NAME}")
                report(done, failed, "Decrypted")
        except (OSError, ValueError) as e:
            run(("error", str(e)), lambda: None)
        finally:
            refresh()

    def do_wait():
        done, failed = progress_run(vault_files_to_convert(True), True, "Encrypting waiting files")
        report(done, failed, "Encrypted")
        refresh()

    def do_pass():
        pw = ask_passphrase("Change passphrase", "New vault passphrase")
        if pw:
            try:
                change_passphrase(pw)
                E.log_audit("[settings]", "Vault Passphrase Changed", "", f"by {E.USER_NAME}")
                run(("ok", "Vault passphrase changed. Other PCs keep working; new PCs use the new passphrase."),
                    lambda: None)
            except (OSError, ValueError) as e:
                run(("error", str(e)), lambda: None)

    def do_code():
        code = new_recovery_code()
        if ask_recovery_saved(code):
            pw = ask_passphrase("Confirm passphrase", "Re-enter the vault passphrase")
            if pw:
                data = read_key_file()
                if open_key_file(data, pw) is None:
                    run(("error", "That isn't the current vault passphrase."), lambda: None)
                    return
                change_passphrase(pw, code)
                E.log_audit("[settings]", "Recovery Code Replaced", "", f"by {E.USER_NAME}")
                run(("ok", "New recovery code saved. The old one no longer works."), lambda: None)

    chk.configure(command=toggle)
    b_wait.configure(command=do_wait)
    b_pass.configure(command=do_pass)
    b_code.configure(command=do_code)
    refresh()
