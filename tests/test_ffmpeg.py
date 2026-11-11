import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from astrbot_plugin_bilibili_live_chat import media


class FfmpegDiscoveryTests(unittest.TestCase):
    def binary(self, root):
        path = Path(root) / "bin with spaces" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        path.parent.mkdir(parents=True)
        path.touch()
        path.chmod(0o755)
        return path

    def test_explicit_binary_works_without_path_and_handles_wrapping_quotes(self):
        with TemporaryDirectory() as folder:
            binary = self.binary(folder)
            with patch.object(media.shutil, "which", return_value=None):
                self.assertEqual(media.resolve_ffmpeg(f'"{binary}"'), str(binary.resolve()))

    def test_explicit_directory_is_supported(self):
        with TemporaryDirectory() as folder:
            binary = self.binary(folder)
            with patch.object(media.shutil, "which", return_value=None):
                self.assertEqual(media.resolve_ffmpeg(str(binary.parent)), str(binary.resolve()))

    def test_invalid_explicit_path_is_reported_instead_of_using_another_binary(self):
        with TemporaryDirectory() as folder, \
             patch.object(media.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             self.assertRaisesRegex(media.MediaError, "配置的 FFmpeg"):
            media.resolve_ffmpeg(str(Path(folder) / "missing" / "ffmpeg"))

    def test_winget_is_found_when_astrbot_has_an_old_path(self):
        with TemporaryDirectory() as folder:
            binary = Path(folder) / "Microsoft/WinGet/Packages/Gyan.FFmpeg_test/ffmpeg-9/bin/ffmpeg.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.object(media.sys, "platform", "win32"), \
                 patch.dict(os.environ, {"LOCALAPPDATA": folder}), \
                 patch.object(media.shutil, "which", return_value=None):
                self.assertEqual(media.resolve_ffmpeg(), str(binary.resolve()))

    def test_no_binary_explains_remote_environment(self):
        with patch.object(media.shutil, "which", return_value=None), \
             patch.object(media, "ffmpeg_candidates", return_value=[]), \
             patch.object(media, "get_ffmpeg_exe", side_effect=RuntimeError), \
             self.assertRaisesRegex(media.MediaError, "AstrBot 所在"):
            media.resolve_ffmpeg()

    def test_bundled_ffmpeg_is_used_without_path_or_a_system_install(self):
        with TemporaryDirectory() as folder:
            binary = self.binary(folder)
            with patch.object(media.shutil, "which", return_value=None), \
                 patch.object(media, "ffmpeg_candidates", return_value=[]), \
                 patch.object(media, "get_ffmpeg_exe", return_value=str(binary)):
                self.assertEqual(media.resolve_ffmpeg(), str(binary.resolve()))

    def test_same_audio_and_video_dependency_error_is_shown_once(self):
        runtime = media.MediaRuntime(media.MediaSettings(), None, 100, None, None)
        runtime.errors.update(audio="缺少 FFmpeg", video="缺少 FFmpeg")
        self.assertEqual(runtime.snapshot()["issues"], ["缺少 FFmpeg"])
