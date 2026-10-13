import queue
import shutil
import tempfile
import unittest
from pathlib import Path

from ott import runner
from ott.sandbox import DockerSandbox, LocalSandbox, make_sandbox

DOCKER = DockerSandbox()
DOCKER_OK, DOCKER_WHY = DOCKER.available()

ECHO = r'''#include <iostream>
#include <string>
int main() { std::string l; while (std::getline(std::cin, l)) { std::cout << "echo:" << l << std::endl; } }
'''
PROBES = {
    "net": r'''#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>
#include <iostream>
int main() { int s = socket(AF_INET, SOCK_STREAM, 0); sockaddr_in a{}; a.sin_family = AF_INET; a.sin_port = htons(80);
  inet_pton(AF_INET, "1.1.1.1", &a.sin_addr); int r = (s < 0) ? -1 : connect(s, (sockaddr*)&a, sizeof a);
  std::cout << (r == 0 ? "NET_OPEN" : "NET_BLOCKED") << std::endl; }''',
    "fs": r'''#include <cstdio>
#include <iostream>
int main() { FILE* a = fopen("/etc/ott-probe", "w"); FILE* b = fopen("/bot/ott-probe", "w"); FILE* c = fopen("/tmp/ott-probe", "w");
  std::cout << (!a && !b && c ? "FS_READONLY" : "FS_WRITABLE") << std::endl; }''',
    "user": r'''#include <unistd.h>
#include <iostream>
int main() { std::cout << (getuid() == 0 ? "ROOT" : "NONROOT") << std::endl; }''',
    "pids": r'''#include <sys/wait.h>
#include <unistd.h>
#include <iostream>
int main() { int ok = 0; for (int i = 0; i < 300; i++) { pid_t p = fork(); if (p == 0) { sleep(5); _exit(0); } if (p > 0) ok++; }
  std::cout << (ok < 100 ? "PIDS_LIMITED" : "PIDS_UNLIMITED") << std::endl; _exit(0); }''',
    "mem": r'''#include <cstring>
#include <iostream>
#include <vector>
int main() { std::vector<char*> blocks; for (int i = 0; i < 40; i++) { char* p = new char[64 << 20]; memset(p, 1, 64 << 20); blocks.push_back(p); }
  std::cout << "ALLOC_OK" << std::endl; }''',
}


def read_line(bot: runner.BotProcess, timeout: float = 20.0):
    try:
        raw = bot.q.get(timeout=timeout)
    except queue.Empty:
        return "TIMEOUT"
    return None if raw is None else raw.decode().strip()


class SandboxUnitTests(unittest.TestCase):
    def test_docker_flags_isolate_bots(self):
        args = DockerSandbox("img").bot_argv(Path("/tmp/bot"), "c1")
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=256m",
                     "--memory-swap=256m", "--cpus=1", "--pids-limit=64", "--user=65534:65534"):
            self.assertIn(flag, args)
        self.assertIn("-i", args)
        self.assertIn("--rm", args)
        self.assertEqual(args[-2:], ["img", "/bot/bot"])
        self.assertTrue(any(a.startswith("--tmpfs=/tmp:rw,noexec") for a in args))
        self.assertTrue(any("dst=/bot/bot,readonly" in a for a in args))

    def test_make_sandbox(self):
        self.assertEqual(make_sandbox("local").kind, "local")
        self.assertEqual(make_sandbox("docker").kind, "docker")
        self.assertIn(make_sandbox("auto").kind, ("local", "docker"))
        with self.assertRaises(ValueError):
            make_sandbox("nope")


@unittest.skipUnless(DOCKER_OK, f"Docker runner unavailable: {DOCKER_WHY}")
class DockerSandboxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ott-sbx-"))
        runner.set_sandbox(DOCKER)

    @classmethod
    def tearDownClass(cls):
        runner.set_sandbox(LocalSandbox())
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, name: str, source: str) -> Path:
        out = self.tmp / name / "bot"
        ok, msg = DOCKER.compile(out, source)
        self.assertTrue(ok, msg)
        return out

    def probe(self, name: str, timeout: float = 30.0):
        bot = runner.BotProcess(self.build(name, PROBES[name]), name)
        try:
            bot.wait_ready()
            bot.send("go")
            return read_line(bot, timeout)
        finally:
            bot.kill()

    def test_compile_error_is_reported_without_host_paths(self):
        ok, msg = DOCKER.compile(self.tmp / "bad" / "bot", "int main( { }")
        self.assertFalse(ok)
        self.assertIn("bot.cpp", msg)
        self.assertNotIn("/src/", msg)

    def test_interactive_echo(self):
        bot = runner.BotProcess(self.build("echo", ECHO), "echo")
        try:
            bot.wait_ready()
            for word in ("one", "two"):
                bot.send(word)
                self.assertEqual(read_line(bot), f"echo:{word}")
        finally:
            bot.kill()

    def test_no_network(self):
        self.assertEqual(self.probe("net"), "NET_BLOCKED")

    def test_read_only_filesystem(self):
        self.assertEqual(self.probe("fs"), "FS_READONLY")

    def test_runs_as_non_root(self):
        self.assertEqual(self.probe("user"), "NONROOT")

    def test_pid_limit(self):
        self.assertEqual(self.probe("pids"), "PIDS_LIMITED")

    def test_memory_limit_kills_bot(self):
        # 40 x 64 MiB would need 2.5 GiB; the 256 MiB cap must kill the container before it prints
        self.assertIsNone(self.probe("mem"))

    def test_kill_removes_container(self):
        bot = runner.BotProcess(self.build("echo2", ECHO), "echo2")
        bot.wait_ready()
        name = bot.container
        bot.kill()
        import subprocess
        import time
        for _ in range(40):
            res = subprocess.run(["docker", "ps", "-q", "--filter", f"name={name}"], capture_output=True, text=True)
            if not res.stdout.strip():
                return
            time.sleep(0.25)
        self.fail("container still running after kill")


if __name__ == "__main__":
    unittest.main()
