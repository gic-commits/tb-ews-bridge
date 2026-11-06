#!/usr/bin/env python3
"""
DavMail IMAP Archive Filter Proxy
监听 17143 端口，透传所有 IMAP 流量，仅过滤 LIST/LSUB 响应，
只保留 /archive/ 前缀的文件夹，拦截对非存档文件夹的操作。
"""

import socket
import threading
import re
import sys
import signal
import configparser
import os
import time

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'davmail_filter.conf')
LOG_FILE = '/tmp/davmail_filter.log'
LOG_MAX_SIZE = 10 * 1024 * 1024  # 10MB
LOG_BACKUP_COUNT = 3


def log_msg(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        if os.path.exists(LOG_FILE):
            if os.path.getsize(LOG_FILE) >= LOG_MAX_SIZE:
                for i in range(LOG_BACKUP_COUNT - 1, 0, -1):
                    old = f'{LOG_FILE}.{i}'
                    if os.path.exists(old):
                        os.remove(old)
                if os.path.exists(LOG_FILE):
                    os.rename(LOG_FILE, f'{LOG_FILE}.1')
        with open(LOG_FILE, 'a') as f:
            f.write(f'[{ts}] {msg}\n')
    except Exception:
        pass


def load_config(config_path):
    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')

    upstream_host = config.get('davmail_filter', 'upstream_host', fallback='127.0.0.1')
    upstream_port = config.getint('davmail_filter', 'upstream_port', fallback=1143)
    listen_port = config.getint('davmail_filter', 'listen_port', fallback=17143)

    mode = config.get('davmail_filter', 'mode', fallback='whitelist').strip().lower()

    rules = []
    for line in config.get('davmail_filter', 'rules', fallback='').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('prefix:'):
            rules.append(('prefix', line[len('prefix:'):].encode('utf-8')))
        elif line.startswith('exact:'):
            rules.append(('exact', line[len('exact:'):].encode('utf-8')))
        elif line.startswith('regex:'):
            pattern = line[len('regex:'):]
            if pattern.startswith('/') and pattern.endswith('/'):
                pattern = pattern[1:-1]
            rules.append(('regex', re.compile(pattern.encode('utf-8'))))
        else:
            rules.append(('prefix', line.encode('utf-8')))

    return {
        'upstream_host': upstream_host,
        'upstream_port': upstream_port,
        'listen_port': listen_port,
        'mode': mode,
        'rules': rules,
    }


def should_keep_folder(folder_name: bytes, config: dict):
    mode = config['mode']
    rules = config['rules']
    matched = False
    for rule_type, rule_value in rules:
        if rule_type == 'prefix':
            if folder_name.startswith(rule_value):
                matched = True
                break
        elif rule_type == 'exact':
            if folder_name == rule_value:
                matched = True
                break
        elif rule_type == 'regex':
            if rule_value.search(folder_name):
                matched = True
                break
    return matched if mode == 'whitelist' else not matched


LIST_RE = re.compile(rb'\* list \((.*?)\) "(.*?)" "(.*)"\r\n', re.IGNORECASE)
LSUB_RE = re.compile(rb'\* lsub \((.*?)\) "(.*?)" "(.*)"\r\n', re.IGNORECASE)
CLIENT_LIST_RE = re.compile(rb'^[A-Za-z0-9]+ LIST ', re.IGNORECASE)
CLIENT_LSUB_RE = re.compile(rb'^[A-Za-z0-9]+ LSUB ', re.IGNORECASE)
CLIENT_FOLDER_OP_RE = re.compile(rb'^[A-Za-z0-9]+ (SELECT|EXAMINE|STATUS|APPEND|STORE|SEARCH|UID|CREATE|DELETE|RENAME|SUBSCRIBE|UNSUBSCRIBE) ', re.IGNORECASE)
LIST_COMPLETION_RE = re.compile(rb'^[A-Za-z0-9]+ (OK|NO|BAD|PREAUTH|BYE) .*(LIST|LSUB)', re.IGNORECASE)


def filter_list_line(line: bytes, config: dict):
    if not (line.startswith(b'* LIST ') or line.startswith(b'* list ') or
            line.startswith(b'* LSUB ') or line.startswith(b'* lsub ')):
        return line

    m = LIST_RE.match(line)
    if not m:
        m = LSUB_RE.match(line)
    if not m:
        return line

    folder_name = m.group(3)
    if should_keep_folder(folder_name, config):
        return line
    return None


def is_folder_command_blocked(line: bytes, config: dict):
    m = CLIENT_FOLDER_OP_RE.match(line)
    if not m:
        return False, None

    folder_match = re.search(rb'"(.*?)"', line)
    if not folder_match:
        return False, None

    folder_name = folder_match.group(1)
    if not should_keep_folder(folder_name, config):
        return True, folder_name
    return False, None


def make_empty_response(tag: bytes) -> bytes:
    return tag + b' OK [READ-WRITE] SELECT completed\r\n'


class ClientToServerForwarder(threading.Thread):
    def __init__(self, src: socket.socket, dst: socket.socket, stop_event: threading.Event,
                 config: dict):
        super().__init__(daemon=True)
        self.src = src
        self.dst = dst
        self.stop_event = stop_event
        self.config = config

    def run(self):
        buf = b''
        try:
            while not self.stop_event.is_set():
                try:
                    data = self.src.recv(4096)
                except (OSError, ConnectionError):
                    break
                if not data:
                    break

                buf += data
                while b'\r\n' in buf:
                    line, buf = buf.split(b'\r\n', 1)
                    line += b'\r\n'

                    log_line = line.rstrip(b'\r\n').decode(errors='replace')
                    log_msg(f"C->S: {log_line}")

                    blocked, folder_name = is_folder_command_blocked(line, self.config)
                    if blocked:
                        tag = line.split(b' ', 1)[0]
                        resp = tag + b' OK [READ-WRITE] SELECT completed\r\n'
                        self.src.sendall(resp)
                        log_msg(f"C->S [BLOCKED->EMPTY]: {tag} OK [empty folder] {folder_name.decode(errors='replace')}")
                        continue

                    self.dst.sendall(line)
        except Exception as e:
            log_msg(f"C2S error: {e}")
        finally:
            self.stop_event.set()
            try:
                self.src.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.src.close()
            except Exception:
                pass
            try:
                self.dst.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.dst.close()
            except Exception:
                pass


class ServerToClientForwarder(threading.Thread):
    def __init__(self, src: socket.socket, dst: socket.socket, stop_event: threading.Event,
                 config: dict):
        super().__init__(daemon=True)
        self.src = src
        self.dst = dst
        self.stop_event = stop_event
        self.config = config
        self.list_responses = []

    def run(self):
        buf = b''
        try:
            while not self.stop_event.is_set():
                try:
                    data = self.src.recv(4096)
                except (OSError, ConnectionError):
                    break
                if not data:
                    break

                buf += data
                while b'\r\n' in buf:
                    line, buf = buf.split(b'\r\n', 1)
                    line += b'\r\n'

                    log_line = line.rstrip(b'\r\n').decode(errors='replace')
                    log_msg(f"S->C: {log_line}")

                    if LIST_COMPLETION_RE.match(line):
                        self.list_responses = []
                        self.dst.sendall(line)
                        continue

                    if line.startswith(b'* LIST ') or line.startswith(b'* list ') or \
                       line.startswith(b'* LSUB ') or line.startswith(b'* lsub '):
                        filtered = filter_list_line(line, self.config)
                        if filtered is not None:
                            self.list_responses.append(filtered)
                            self.dst.sendall(filtered)
                        continue

                    self.dst.sendall(line)
        except Exception as e:
            log_msg(f"S2C error: {e}")
        finally:
            self.stop_event.set()
            try:
                self.src.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.src.close()
            except Exception:
                pass
            try:
                self.dst.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.dst.close()
            except Exception:
                pass


def handle_client(client_sock: socket.socket, config: dict):
    upstream = None
    try:
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.connect((config['upstream_host'], config['upstream_port']))
    except Exception as e:
        print(f"[ERR] 无法连接上游 {config['upstream_host']}:{config['upstream_port']}: {e}")
        try:
            client_sock.close()
        except Exception:
            pass
        return

    stop_event = threading.Event()
    t_c2s = ClientToServerForwarder(client_sock, upstream, stop_event, config)
    t_s2c = ServerToClientForwarder(upstream, client_sock, stop_event, config)

    t_c2s.start()
    t_s2c.start()

    t_c2s.join()
    t_s2c.join()


def main():
    config = load_config(CONFIG_FILE)
    with open(LOG_FILE, 'w') as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] DavMail Filter Proxy started\n")

    print(f"[INFO] DavMail IMAP Archive Filter 启动中...")
    print(f"[INFO] 监听端口: {config['listen_port']}")
    print(f"[INFO] 上游地址: {config['upstream_host']}:{config['upstream_port']}")
    print(f"[INFO] 过滤模式: {config['mode']}")
    for rule_type, rule_value in config['rules']:
        if rule_type == 'regex':
            print(f"[INFO]   规则: regex {rule_value.pattern.decode()}")
        else:
            print(f"[INFO]   规则: {rule_type}:{rule_value.decode()}")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(('0.0.0.0', config['listen_port']))
    except OSError as e:
        print(f"[ERR] 绑定端口 {config['listen_port']} 失败: {e}")
        sys.exit(1)
    server.listen(16)
    print(f"[INFO] 已就绪，等待客户端连接...")

    def handle_sigterm(signum, frame):
        print("\n[INFO] 收到终止信号，正在关闭...")
        try:
            server.close()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    try:
        while True:
            try:
                client_sock, addr = server.accept()
            except OSError as e:
                log_msg(f"accept error: {e}")
                continue
            print(f"[INFO] 新连接来自 {addr[0]}:{addr[1]}")
            try:
                t = threading.Thread(target=handle_client, args=(client_sock, config), daemon=True)
                t.start()
            except Exception as e:
                log_msg(f"spawn thread error: {e}")
                try:
                    client_sock.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()