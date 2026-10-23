"""Fixed compiler and runtime profiles shared by the submission services."""

import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO


@dataclass(frozen=True)
class Language:
    label: str
    filename: str
    compiler: str
    flags: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()
    compile_timeout_seconds: int = 15
    compile_tmp_mb: int = 64
    artifact_format: str = "native"
    main_class: str | None = None
    runtime: tuple[str, ...] = ()

    def command(self, source: str, output: str) -> list[str]:
        if self.artifact_format == "jar":
            return [self.compiler, *self.flags, "-d", output, source]
        return [self.compiler, *self.flags, "-o", output, source]

    def package_command(self, classes: str, output: str) -> list[str]:
        if self.artifact_format != "jar" or not self.main_class:
            raise ValueError("This language does not package a JAR.")
        return ["jar", "--create", "--file", output, "--main-class", self.main_class, "-C", classes, "."]

    def run_command(self, artifact: str) -> list[str]:
        return [*self.runtime, artifact] if self.runtime else [artifact]


LANGUAGES = MappingProxyType({
    "cpp17": Language("C++17", "bot.cpp", "g++", ("-std=c++17", "-O2", "-pipe")),
    "c17": Language("C17", "bot.c", "gcc", ("-std=c17", "-O2", "-pipe")),
    "rust2021": Language("Rust 2021", "bot.rs", "rustc",
                         ("--edition=2021", "--crate-type=bin", "-C", "opt-level=2", "-C", "panic=abort")),
    "go": Language("Go", "bot.go", "go",
                   ("build", "-trimpath", "-buildvcs=false", "-ldflags=-s -w"),
                   (("CGO_ENABLED", "0"), ("GO111MODULE", "off"), ("GOTOOLCHAIN", "local"),
                    ("GOPROXY", "off"), ("GOSUMDB", "off")),
                   compile_timeout_seconds=30, compile_tmp_mb=256),
    "java17": Language("Java 17", "Main.java", "javac",
                       ("--release", "17", "-encoding", "UTF-8", "-proc:none"),
                       compile_timeout_seconds=45, compile_tmp_mb=512, artifact_format="jar", main_class="Main",
                       runtime=("java", "-XX:ActiveProcessorCount=1", "-XX:+ExitOnOutOfMemoryError", "-jar")),
})


def valid_jar(path: Path | BinaryIO, main_class: str) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            manifests = [item for item in infos if item.filename == "META-INF/MANIFEST.MF"]
            if (len(manifests) != 1 or manifests[0].file_size > 65536 or len(infos) > 4096
                    or names.count(main_class.replace(".", "/") + ".class") != 1):
                return False
            text = archive.read(manifests[0]).decode("utf-8")
            return f"Main-Class: {main_class}" in text.splitlines()
    except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError):
        return False


def language_profile(language: str = "cpp17") -> Language:
    if not isinstance(language, str) or language not in LANGUAGES:
        raise ValueError("Unsupported language; choose " + ", ".join(LANGUAGES) + ".")
    return LANGUAGES[language]


def build_key(source_hash: str, language: str = "cpp17") -> str:
    language_profile(language)
    # Preserve existing C++ cache identities during migration.
    return source_hash if language == "cpp17" else language + "-" + source_hash
