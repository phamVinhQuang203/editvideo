"""Launcher F2 biệt lập; nhận cookie qua stdin để không lộ trong process list/log."""
import runpy
import sys


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: douyin_f2_download.py URL OUTPUT_DIR")
    url, output_dir = sys.argv[1:]
    cookie = sys.stdin.read().strip()
    if not cookie:
        raise SystemExit("Missing Douyin cookie")
    sys.argv = ["f2", "dy", "-u", url, "-M", "one", "-p", output_dir,
                "-f", "false", "-m", "false", "-v", "false", "-d", "false",
                "-n", "{aweme_id}", "-l", "en_US", "-k", cookie]
    runpy.run_module("f2", run_name="__main__")


if __name__ == "__main__":
    main()
