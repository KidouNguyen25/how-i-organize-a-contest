import asyncio
import datetime
import json
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path

from judge.database import Database
from judge.judge_engine import DockerJudge
from judge.problems import ProblemSet
from judge.protocol import PROTOCOL_VERSION
from judge.server import JudgeService, make_tls_context


OPENSSL = shutil.which("openssl")
try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:
    x509 = None


@unittest.skipUnless(OPENSSL or x509, "OpenSSL or cryptography is required to generate test certificate")
class TLSIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _generate_certificate(cert, key):
        if x509 is not None:
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.datetime.now(datetime.timezone.utc)
            certificate = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(private_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(
                    x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                    critical=False,
                )
                .sign(private_key, hashes.SHA256())
            )
            cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key.write_bytes(
                private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                )
            )
            return
        subprocess.run(
            [
                OPENSSL,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def test_tls_connection_uses_validated_server_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "server.crt"
            key = Path(directory) / "server.key"
            await asyncio.to_thread(self._generate_certificate, cert, key)
            server_context = make_tls_context(cert, key)
            self.assertEqual(server_context.minimum_version, ssl.TLSVersion.TLSv1_2)
            service = JudgeService(
                Database(Path(directory) / "judge.sqlite3"),
                ProblemSet(Path(__file__).resolve().parents[1] / "problems"),
                DockerJudge(),
            )
            server = await asyncio.start_server(
                service.handle_connection,
                "127.0.0.1",
                0,
                ssl=server_context,
                limit=1024 * 1024 + 1,
            )
            client_context = ssl.create_default_context(cafile=str(cert))
            client_context.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1",
                    server.sockets[0].getsockname()[1],
                    ssl=client_context,
                    server_hostname="localhost",
                    limit=1024 * 1024 + 1,
                )
                self.assertIn(writer.get_extra_info("ssl_object").version(), {"TLSv1.2", "TLSv1.3"})
                writer.write(
                    json.dumps(
                        {
                            "version": PROTOCOL_VERSION,
                            "request_id": "tls-check",
                            "op": "LIST_PROBLEMS",
                            "token": None,
                            "data": {},
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
                await writer.drain()
                response = json.loads((await reader.readline()).decode("utf-8"))
                self.assertTrue(response["ok"])
                self.assertEqual(response["request_id"], "tls-check")
                writer.close()
                await writer.wait_closed()
            finally:
                server.close()
                await server.wait_closed()


if __name__ == "__main__":
    unittest.main()

