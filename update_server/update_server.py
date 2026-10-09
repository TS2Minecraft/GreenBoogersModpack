#!/usr/bin/env python3
"""
packwiz-server-updater
Синхронизирует серверный модпак (packwiz) с игровым сервером через SFTP.

Запускается ЛОКАЛЬНО. Не требует pre-launch команд на хостинге.
Настройки берутся из файла .env рядом со скриптом.
"""

import os
import sys
import time
import shutil
import subprocess
import urllib.request
import urllib.parse
import posixpath
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit("Установите paramiko:  pip install paramiko")

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("Установите python-dotenv:  pip install python-dotenv")


# ======================= ЗАГРУЗКА .env =======================
ENV_FILE = Path(__file__).resolve().parent / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
    print(f"[*] Загружены настройки из {ENV_FILE.name}")
else:
    print(f"[!] Файл {ENV_FILE.name} не найден, использую переменные окружения / значения по умолчанию.")
# =============================================================

def _normalize_sftp_host(raw: str):
    """Принимает 'sftp://host:port', 'host:port' или 'host' и возвращает (host, port_or_None)."""
    raw = raw.strip()
    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        return parsed.hostname, parsed.port
    if ":" in raw and not raw.startswith("["):  # не IPv6
        host, _, port = raw.rpartition(":")
        if port.isdigit():
            return host, int(port)
    return raw, None

# ======================= CONFIG =======================
PACKWIZ_URL   = os.environ.get("PACKWIZ_URL",   "https://example.com/pack.toml")

_raw_host = os.environ.get("SFTP_HOST", "your-server.gamely.pro")
_host, _host_port = _normalize_sftp_host(_raw_host)

SFTP_HOST     = _host or "your-server.gamely.pro"
SFTP_PORT     = int(os.environ.get("SFTP_PORT", str(_host_port or 2022)))
SFTP_USER     = os.environ.get("SFTP_USER",     "your-username")
SFTP_PASSWORD = os.environ.get("SFTP_PASSWORD", "")
SFTP_KEY_PATH = os.environ.get("SFTP_KEY_PATH", "")

REMOTE_DIR    = os.environ.get("REMOTE_DIR",    ".")
CLEAN_STAGING     = os.environ.get("CLEAN_STAGING", "true").lower() == "false"
PACKWIZ_RETRIES   = int(os.environ.get("PACKWIZ_RETRIES", "5"))
PACKWIZ_RETRY_DELAY = int(os.environ.get("PACKWIZ_RETRY_DELAY", "10"))

STAGING_DIR   = Path(os.environ.get("STAGING_DIR", ".packwiz-staging"))
MANIFEST      = Path(".packwiz-sync-manifest.txt")
BOOTSTRAP_JAR = Path("packwiz-installer-bootstrap.jar")
BOOTSTRAP_URL = (
    "https://github.com/packwiz/packwiz-installer-bootstrap/"
    "releases/latest/download/packwiz-installer-bootstrap.jar"
)
# ======================================================


def check_config():
    """Проверяем, что критичные параметры заданы."""
    errors = []
    if not PACKWIZ_URL.startswith(("http://", "https://")):
        errors.append("PACKWIZ_URL — должна быть http(s)-ссылка на pack.toml")
    if SFTP_HOST.startswith("your-"):
        errors.append("SFTP_HOST — укажите адрес из панели Gamely.pro")
    if SFTP_USER.startswith("your-"):
        errors.append("SFTP_USER — укажите ваш SFTP-логин")
    if not SFTP_PASSWORD and not SFTP_KEY_PATH:
        errors.append("SFTP_PASSWORD или SFTP_KEY_PATH — задайте один из них")
    if errors:
        print("\n[!] Проверьте .env, не заполнено:")
        for e in errors:
            print(f"    - {e}")
        sys.exit(1)


def download_bootstrap():
    if BOOTSTRAP_JAR.exists():
        print(f"[*] {BOOTSTRAP_JAR.name} уже есть.")
        return
    print(f"[*] Скачиваю {BOOTSTRAP_JAR.name}...")
    urllib.request.urlretrieve(BOOTSTRAP_URL, BOOTSTRAP_JAR)
    print("[+] Готово.")


def run_packwiz():
    # Чистим staging только если явно попросили или его нет
    if STAGING_DIR.exists() and CLEAN_STAGING:
        print(f"[*] Очищаю {STAGING_DIR}...")
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    bootstrap_abs = BOOTSTRAP_JAR.resolve()

    for attempt in range(1, PACKWIZ_RETRIES + 1):
        print(f"[*] Попытка {attempt}/{PACKWIZ_RETRIES}: запуск packwiz-installer -> {STAGING_DIR}...")
        proc = subprocess.run(
            [
                "java", "-jar", str(bootstrap_abs),
                "-g",
                "-s", "server",
                PACKWIZ_URL,
            ],
            capture_output=True, text=True,
            cwd=STAGING_DIR,
        )
        if proc.stdout:
            print(proc.stdout.rstrip())
        if proc.returncode == 0:
            print("[+] packwiz-installer завершился успешно.")
            return
        print(proc.stderr, file=sys.stderr)
        print(f"[!] Попытка {attempt} не удалась (код {proc.returncode}).")
        if attempt < PACKWIZ_RETRIES:
            print(f"[*] Пауза {PACKWIZ_RETRY_DELAY} сек. и повтор...")
            time.sleep(PACKWIZ_RETRY_DELAY)

    sys.exit("[!] packwiz-installer не смог скачать модпак после всех попыток.")

def connect_sftp():
    print(f"[*] Подключаюсь к {SFTP_HOST}:{SFTP_PORT} как {SFTP_USER}...")
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))

    if SFTP_KEY_PATH:
        key_path = Path(SFTP_KEY_PATH).expanduser()
        last_err = None
        for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                key = cls.from_private_key_file(str(key_path))
                transport.connect(username=SFTP_USER, pkey=key)
                break
            except Exception as e:
                last_err = e
        else:
            raise SystemExit(f"[!] Не удалось использовать ключ: {last_err}")
    else:
        transport.connect(username=SFTP_USER, password=SFTP_PASSWORD)

    sftp = paramiko.SFTPClient.from_transport(transport)
    return sftp, transport


def ensure_remote_dir(sftp, path):
    if path in ("", ".", "/"):
        return
    parts = []
    p = path
    while p not in ("", ".", "/"):
        parts.append(p)
        p = posixpath.dirname(p)
    for d in reversed(parts):
        try:
            sftp.stat(d)
        except IOError:
            try:
                sftp.mkdir(d)
            except IOError:
                pass


def walk_local(base: Path):
    result = {}
    for root, _, files in os.walk(base):
        for f in files:
            abs_path = Path(root) / f
            rel = abs_path.relative_to(base).as_posix()
            result[rel] = abs_path
    return result


def remote_join(base, rel):
    return rel if base in ("", ".") else posixpath.join(base, rel)


def load_manifest():
    if not MANIFEST.exists():
        return set()
    return {line for line in MANIFEST.read_text().splitlines() if line.strip()}


def save_manifest(paths):
    MANIFEST.write_text("\n".join(sorted(paths)))


def sync(sftp, previously_managed: set):
    local_files = walk_local(STAGING_DIR)
    local_set = set(local_files.keys())

    to_upload = []
    for rel, abs_path in local_files.items():
        rp = remote_join(REMOTE_DIR, rel)
        try:
            rstat = sftp.stat(rp)
            if rstat.st_size != abs_path.stat().st_size:
                to_upload.append((rel, abs_path))
        except IOError:
            to_upload.append((rel, abs_path))

    to_delete = sorted(previously_managed - local_set)

    print(f"[*] Файлов в модпаке: {len(local_files)}")
    print(f"[*] К загрузке: {len(to_upload)}   К удалению: {len(to_delete)}")

    for rel, abs_path in to_upload:
        rp = remote_join(REMOTE_DIR, rel)
        ensure_remote_dir(sftp, posixpath.dirname(rp))
        print(f"  ↑ {rp}")
        sftp.put(str(abs_path), rp)

    for rel in to_delete:
        rp = remote_join(REMOTE_DIR, rel)
        print(f"  ✗ {rp}")
        try:
            sftp.remove(rp)
        except IOError as e:
            print(f"    (не удалить: {e})")

    save_manifest(local_set)


def main():
    check_config()
    download_bootstrap()
    run_packwiz()

    sftp, transport = connect_sftp()
    try:
        sync(sftp, load_manifest())
    finally:
        sftp.close()
        transport.close()
        input("Press Enter...")

    print("\n[✓] Готово. Перезапустите сервер через панель Gamely.pro.")


if __name__ == "__main__":
    main()
