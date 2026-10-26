import os
import shutil
import subprocess
import unittest
from pathlib import Path

from judge.judge_engine import DockerJudge
from judge.problems import Problem, TestCase


IMAGE = "lan-judge-cpp17"
RUNTIME_IMAGE = "lan-judge-runtime"
DOCKER = shutil.which("docker")


def _image_ready():
    if not DOCKER or os.name != "posix":
        return False
    try:
        return subprocess.run(
            [DOCKER, "image", "inspect", IMAGE, RUNTIME_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


IMAGE_READY = _image_ready()


class SandboxCommandTests(unittest.TestCase):
    def test_container_flags_enforce_basic_isolation_and_limits(self):
        command = DockerJudge()._container_base(
            cid_file=Path("job.cid"),
            memory_mb=256,
            uid=65534,
            gid=65534,
            tmp_size_mb=16,
        )
        self.assertIn("--network=none", command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--security-opt=no-new-privileges", command)
        self.assertIn("--memory=256m", command)
        self.assertIn("--pids-limit=64", command)
        self.assertTrue(any(argument.startswith("--tmpfs=/tmp:") for argument in command))


@unittest.skipUnless(IMAGE_READY, "Linux Docker daemon and runner image are required")
class DockerJudgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _judge(self, source, tests, time_limit_ms=2000, memory_limit_mb=256, language="cpp17"):
        problem = Problem(
            id="fixture",
            title="Fixture",
            statement="Fixture problem",
            sample_input="",
            sample_output="",
            time_limit_ms=time_limit_ms,
            memory_limit_mb=memory_limit_mb,
            compile_timeout_sec=15,
            tests=tuple(tests),
        )
        return await DockerJudge(IMAGE, RUNTIME_IMAGE).judge(problem, source, language)

    async def test_c17_and_rust2021_verdicts_use_separate_runtime(self):
        sources = {
            "c17": '#include <stdio.h>\nint main(void){int n;if(scanf("%d", &n)!=1)return 1;printf("%d\\n",n);}',
            "rust2021": 'use std::io::{self,Read}; fn main(){let mut s=String::new();io::stdin().read_to_string(&mut s).unwrap();let n:i32=s.trim().parse().unwrap();println!("{}",n);}',
        }
        for language, source in sources.items():
            with self.subTest(language=language):
                accepted = await self._judge(source, [TestCase("37\n", "37\n", 100)], language=language)
                self.assertEqual((accepted["verdict"], accepted["score"]), ("AC", 100))
                wrong = await self._judge(source, [TestCase("37\n", "38\n", 100)], language=language)
                self.assertEqual((wrong["verdict"], wrong["score"]), ("WA", 0))
                error = await self._judge("invalid source !", [TestCase("", "", 100)], language=language)
                self.assertEqual(error["verdict"], "CE")

    async def test_cpp17_verdicts_and_partial_points(self):
        cases = [TestCase("1\n", "1\n", 40), TestCase("2\n", "2\n", 60)]
        accepted = await self._judge(
            '#include <iostream>\nint main(){int n;std::cin>>n;std::cout<<n<<"\\n";}',
            cases,
        )
        self.assertEqual((accepted["verdict"], accepted["score"]), ("AC", 100))

        partial = await self._judge(
            '#include <iostream>\nint main(){int n;std::cin>>n;std::cout<<(n==1?1:0)<<"\\n";}',
            cases,
        )
        self.assertEqual((partial["verdict"], partial["score"]), ("PARTIAL", 40))

        wrong = await self._judge(
            '#include <iostream>\nint main(){std::cout<<0<<"\\n";}',
            [TestCase("1\n", "1\n", 100)],
        )
        self.assertEqual(wrong["verdict"], "WA")

        compile_error = await self._judge(
            "int main( { this does not compile",
            [TestCase("", "", 100)],
        )
        self.assertEqual(compile_error["verdict"], "CE")

        runtime_error = await self._judge(
            "int main(){return 7;}",
            [TestCase("", "", 100)],
        )
        self.assertEqual(runtime_error["verdict"], "RE")

        time_limit = await self._judge(
            "int main(){for(;;){} }",
            [TestCase("", "", 100)],
            time_limit_ms=100,
        )
        self.assertEqual(time_limit["verdict"], "TLE")

    async def test_network_is_disabled_inside_runtime_container(self):
        source = (
            "#include <arpa/inet.h>\n"
            "#include <sys/socket.h>\n"
            "#include <unistd.h>\n"
            "#include <iostream>\n"
            "int main(){int s=socket(AF_INET,SOCK_STREAM,0);sockaddr_in a{};"
            "a.sin_family=AF_INET;a.sin_port=htons(80);inet_pton(AF_INET,\"1.1.1.1\",&a.sin_addr);"
            "int r=connect(s,(sockaddr*)&a,sizeof(a));close(s);"
            "std::cout<<(r==-1?\"blocked\":\"connected\");}"
        )
        result = await self._judge(source, [TestCase("", "blocked", 100)])
        self.assertEqual(result["verdict"], "AC")

    async def test_memory_limit_stops_large_allocation(self):
        source = (
            "#include <vector>\n"
            "int main(){std::vector<char> data(512*1024*1024);"
            "for(auto &value:data)value=1;return 0;}"
        )
        result = await self._judge(source, [TestCase("", "", 100)], memory_limit_mb=64)
        self.assertEqual(result["verdict"], "RE")


if __name__ == "__main__":
    unittest.main()

