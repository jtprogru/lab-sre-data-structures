#!/usr/bin/env python3
"""Изолированные замеры для статьи. Python stdlib, git и Docker CLI."""

import argparse
import bisect
import concurrent.futures
import hashlib
import json
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, data=None, cwd=None):
    return subprocess.run(
        args, input=data, cwd=cwd, capture_output=True, check=True
    ).stdout.decode()


def regex():
    pattern = re.compile(r"^[\s\u200c]+|[\s\u200c]+$")
    rows = []
    # Пробелы внутри строки: второй альтернативе не удастся найти конец строки.
    for n in (5000, 10000, 20000, 40000):
        value = "x" + " " * n + "x"
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            assert pattern.search(value) is None
            samples.append(time.perf_counter() - start)
        rows.append(
            {
                "spaces": n,
                "seconds_median": statistics.median(samples),
                "seconds": samples,
            }
        )
    value = "x" + " " * 40000 + "x"
    start = time.perf_counter()
    for _ in range(10000):
        value.rstrip()
    return {
        "pattern": pattern.pattern,
        "rows": rows,
        "rstrip_seconds_mean": (time.perf_counter() - start) / 10000,
    }


def hashing():
    def digest(value):
        # MD5 здесь нужен для распределения, а не для безопасности.
        return int.from_bytes(hashlib.md5(value.encode()).digest(), "big")

    keys = [digest(f"key:{i}") for i in range(200000)]

    def ring(n):
        points = sorted(
            (digest(f"node:{node}:vnode:{vnode}"), node)
            for node in range(n)
            for vnode in range(160)
        )
        return [point for point, _ in points], [node for _, node in points]

    def owner(key, points, nodes):
        return nodes[bisect.bisect_left(points, key) % len(points)]

    rows = []
    for n in (4, 10):
        old_points, old_nodes = ring(n)
        new_points, new_nodes = ring(n + 1)
        mod = sum(key % n != key % (n + 1) for key in keys) / len(keys)
        moved = sum(
            owner(key, old_points, old_nodes) != owner(key, new_points, new_nodes)
            for key in keys
        ) / len(keys)
        rows.append(
            {
                "from": n,
                "to": n + 1,
                "mod_moved": mod,
                "ring_moved": moved,
                "mod_expected": n / (n + 1),
                "ring_expected": 1 / (n + 1),
            }
        )
    return {
        "keys": len(keys),
        "points_per_node": 160,
        "note": "Кольцо с виртуальными узлами, не побайтовая реализация ketama.",
        "rows": rows,
    }


def bisect_lab():
    with tempfile.TemporaryDirectory(prefix="sre-bisect-") as tmp:
        run("git", "init", "-q", tmp)
        chunks = []
        for i in range(1, 1025):
            message = f"commit {i}\n".encode()
            content = f"{i}\n".encode()
            chunks.append(
                f"commit refs/heads/main\nmark :{i}\nauthor Lab <lab@example.invalid> 1700000000 +0000\ncommitter Lab <lab@example.invalid> 1700000000 +0000\ndata {len(message)}\n".encode()
                + message
                + f"M 100644 inline version\ndata {len(content)}\n".encode()
                + content
                + b"\n"
            )
        run("git", "-C", tmp, "fast-import", "--quiet", data=b"".join(chunks))
        run("git", "-C", tmp, "checkout", "-q", "main")
        check = Path(tmp) / "check.sh"
        check.write_text(
            '#!/bin/sh\necho checked >> checks.log\ntest "$(cat version)" -lt 700\n'
        )
        run("git", "-C", tmp, "bisect", "start", "main", "main~1023")
        output = run("git", "-C", tmp, "bisect", "run", "sh", "./check.sh")
        first_bad = run(
            "git", "-C", tmp, "show", "-s", "--format=%s", "refs/bisect/bad"
        ).strip()
        checks = len((Path(tmp) / "checks.log").read_text().splitlines())
        assert first_bad == "commit 700", output
        return {
            "commits": 1024,
            "first_bad": first_bad,
            "checks": checks,
            "output": output,
        }


class Container:
    def __init__(self, image, *args):
        self.image, self.args, self.cid = image, args, None

    def __enter__(self):
        self.cid = run("docker", "run", "-d", "--rm", *self.args, self.image).strip()
        return self

    def __exit__(self, *_):
        run("docker", "rm", "-f", self.cid)

    def execute(self, *args, data=None):
        return run("docker", "exec", "-i", self.cid, *args, data=data)

    def ready(self, *args):
        for _ in range(120):
            try:
                return self.execute(*args)
            except subprocess.CalledProcessError:
                time.sleep(0.25)
        raise RuntimeError(f"{self.image} not ready")


def nginx():
    rows = {}
    for mode in ("delay", "nodelay", "return"):
        with tempfile.TemporaryDirectory(prefix="sre-nginx-") as tmp:
            conf = Path(tmp) / "nginx.conf"
            action = (
                'return 200 "ok";'
                if mode == "return"
                else "root /usr/share/nginx/html; try_files /index.html =404;"
            )
            conf.write_text(
                "events {}\nhttp {\nlimit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;\nserver { listen 80; location / { limit_req zone=api burst=20 "
                + ("nodelay" if mode == "nodelay" else "")
                + "; "
                + action
                + " } } }\n"
            )
            with Container(
                "nginx:1.31",
                "-p",
                "127.0.0.1::80",
                "-v",
                f"{conf}:/etc/nginx/nginx.conf:ro",
            ) as c:
                c.ready("nginx", "-t")
                endpoint = run("docker", "port", c.cid, "80/tcp").strip()
                barrier = threading.Barrier(30)

                def request(_):
                    barrier.wait(timeout=15)
                    start = time.perf_counter()
                    try:
                        with urllib.request.urlopen(
                            f"http://{endpoint}/", timeout=10
                        ) as response:
                            response.read()
                            code = response.status
                    except urllib.error.HTTPError as error:
                        error.read()
                        code = error.code
                    return {"status": code, "seconds": time.perf_counter() - start}

                with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
                    results = list(pool.map(request, range(30)))
                ok = sorted(
                    item["seconds"] for item in results if item["status"] == 200
                )
                if mode == "return":
                    assert len(ok) == 30, results
                else:
                    # Момент старта потоков влияет на точную границу пачки.
                    assert 20 <= len(ok) <= 23, results
                    assert all(item["status"] in (200, 503) for item in results)
                rows[mode] = {
                    "version": c.execute("sh", "-c", "nginx -v 2>&1"),
                    "accepted": len(ok),
                    "rejected": 30 - len(ok),
                    "accepted_seconds_sorted": ok,
                    "requests": results,
                }
    return rows


def postgres():
    with Container(
        "postgres:18.6", "--network", "none", "-e", "POSTGRES_HOST_AUTH_METHOD=trust"
    ) as c:
        c.ready("pg_isready", "-U", "postgres")

        def sql(query):
            return c.execute(
                "psql",
                "-X",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                data=query.encode(),
            )

        sql("""CREATE TABLE orders (id integer PRIMARY KEY, customer_email text NOT NULL, amount integer NOT NULL);
INSERT INTO orders SELECT i, 'user'||i||'@example.com', i%10000 FROM generate_series(1,5000000) i;
VACUUM ANALYZE orders;
CREATE EXTENSION pageinspect;
""")
        rows = []

        def measure(label, query):
            # Первый проход прогревает кеши; приводим hit/read отдельно.
            sql(query)
            samples = []
            for _ in range(3):
                plan = json.loads(
                    sql(
                        "SET jit=off; SET max_parallel_workers_per_gather=2;\nEXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                        + query
                    ).split("SET\n")[-1]
                )[0]
                samples.append(plan)
            selected = sorted(samples, key=lambda plan: plan["Execution Time"])[1]
            top = selected["Plan"]
            rows.append(
                {
                    "query": label,
                    "execution_ms_median": selected["Execution Time"],
                    "shared_hit": top.get("Shared Hit Blocks", 0),
                    "shared_read": top.get("Shared Read Blocks", 0),
                    "plan": selected,
                    "samples": samples,
                }
            )

        measure("id", "SELECT * FROM orders WHERE id=4242424;")
        measure(
            "email_without_index",
            "SELECT * FROM orders WHERE customer_email='user4242424@example.com';",
        )
        sql("CREATE INDEX orders_email_idx ON orders(customer_email); ANALYZE orders;")
        measure(
            "email_with_index",
            "SELECT * FROM orders WHERE customer_email='user4242424@example.com';",
        )
        return {
            "version": sql("SELECT version();").strip(),
            "table_bytes": int(sql("SELECT pg_relation_size('orders');")),
            "btree_levels": sql(
                "SELECT 'pkey',level FROM bt_metap('orders_pkey') UNION ALL SELECT 'email',level FROM bt_metap('orders_email_idx');"
            ),
            "rows": rows,
        }


def redis():
    def resp(args):
        encoded = [str(arg).encode() for arg in args]
        return f"*{len(encoded)}\r\n".encode() + b"".join(
            f"${len(arg)}\r\n".encode() + arg + b"\r\n" for arg in encoded
        )

    with Container("redis:8.10", "--network", "none") as c:
        c.ready("redis-cli", "ping")
        commands = []
        for start in range(0, 1000000, 1000):
            values = [f"user:{i}" for i in range(start, start + 1000)]
            commands.extend(
                [resp(["PFADD", "hll", *values]), resp(["SADD", "exact", *values])]
            )
        pipe_output = c.execute("redis-cli", "--pipe", data=b"".join(commands))

        def cli(*args):
            return c.execute("redis-cli", "--raw", *args).strip()

        # HLL объединяет множества; пересечение не надо считать дважды.
        cli("PFADD", "a", "x", "y")
        cli("PFADD", "b", "y", "z")
        cli("PFMERGE", "merged", "a", "b")
        assert int(cli("PFCOUNT", "merged")) == 3
        return {
            "info": cli("INFO", "server"),
            "pipe_output": pipe_output,
            "count_hll": int(cli("PFCOUNT", "hll")),
            "count_exact": int(cli("SCARD", "exact")),
            "bytes_hll": int(cli("MEMORY", "USAGE", "hll")),
            "bytes_set": int(cli("MEMORY", "USAGE", "exact")),
            "policy": cli("CONFIG", "GET", "maxmemory-policy"),
            "samples": cli("CONFIG", "GET", "maxmemory-samples"),
        }


def kernel():
    with Container("redis:8.10", "--network", "none") as c:
        return {
            "uname": c.execute("uname", "-a").strip(),
            "memory": c.execute("grep", "MemTotal", "/proc/meminfo").strip(),
            "conntrack": c.execute(
                "sh",
                "-c",
                'for key in count max buckets; do file=/proc/sys/net/netfilter/nf_conntrack_$key; if test -r "$file"; then echo "$key=$(cat "$file")"; else echo "$key=unavailable"; fi; done',
            ),
        }


def main():
    modes = {
        "regex": regex,
        "hashing": hashing,
        "bisect": bisect_lab,
        "nginx": nginx,
        "postgres": postgres,
        "redis": redis,
        "kernel": kernel,
    }
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=[*modes, "all", "local", "docker"])
    args = parser.parse_args()
    selected = (
        list(modes)
        if args.mode == "all"
        else (
            ["regex", "hashing", "bisect"]
            if args.mode == "local"
            else ["nginx", "postgres", "redis", "kernel"]
            if args.mode == "docker"
            else [args.mode]
        )
    )
    for name in selected:
        print(f"Running {name}", flush=True)
        result = {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "host": platform.platform(),
            "python": sys.version,
            "result": modes[name](),
        }
        if name in ("nginx", "postgres", "redis", "kernel"):
            images = {
                "nginx": "nginx:1.31",
                "postgres": "postgres:18.6",
                "redis": "redis:8.10",
                "kernel": "redis:8.10",
            }
            result["image"] = json.loads(
                run(
                    "docker",
                    "image",
                    "inspect",
                    images[name],
                    "--format",
                    "{{json .RepoDigests}}",
                )
            )
        path = ROOT / "results" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"Saved {path}", flush=True)


if __name__ == "__main__":
    main()
