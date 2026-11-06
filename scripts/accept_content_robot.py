"""从候选源码启动本地验收；预检不启动浏览器，不下载驱动或读取 .env。"""

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "offline", "browser"), default="preflight")
    parser.add_argument("--python", type=Path, required=True, help="已有依赖的 Python，不安装任何包")
    parser.add_argument("--driver", type=Path, help="已确认可用的本地 ChromeDriver")
    parser.add_argument("--evidence", type=Path, required=True, help="本次独立证据目录，必须不存在")
    parser.add_argument("--record-video", action="store_true", help="可选录制本地合成UI；缺已有ffmpeg时预检停止，不下载")
    parser.add_argument("--video-dir", type=Path, help="录屏目录，默认在本次evidence/videos下")
    parser.add_argument("--playwright-browsers-path", type=Path, help="已有Playwright编码器缓存，不安装或下载")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--first-case", action="store_true", help="仅运行第一个正常整链用例，供失败诊断")
    selection.add_argument("--cases", nargs="+", choices=["normal-1", "normal-2", "normal-3", "missing_body",
        "missing_tags", "missing_image", "same_title", "save_timeout", "no_login", "challenge", "writeback"],
        help="仅运行指定整链场景，避免重跑已经成功的批次")
    args = parser.parse_args()
    if (args.first_case or args.cases) and args.mode != "browser":
        parser.error("场景选择仅适用于browser诊断")
    if args.record_video and args.mode == "offline" or (args.video_dir or args.playwright_browsers_path) and not args.record_video:
        parser.error("录屏参数仅用于显式--record-video的preflight/browser模式")
    root = Path(__file__).resolve().parents[1]
    python = args.python.absolute()
    if not python.is_file():
        parser.error("--python 必须指向已有解释器")
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    plist = chrome.parents[1] / "Info.plist"
    version = plistlib.loads(plist.read_bytes())["CFBundleShortVersionString"] if plist.is_file() else None
    driver_version = None
    driver = args.driver.absolute() if args.driver else None
    if driver and driver.is_file():
        output = subprocess.run([str(driver), "--version"], capture_output=True, text=True, check=True).stdout
        match = re.search(r"ChromeDriver ([0-9.]+)", output)
        driver_version = match[1] if match else None
    ready = platform.system() == "Darwin" and chrome.is_file() and driver_version is not None and (
        driver_version.split(".")[:3] == version.split(".")[:3])
    video_dir = (args.video_dir or evidence / "videos").resolve()
    if not video_dir.is_relative_to(evidence):
        parser.error("--video-dir 必须位于本次evidence目录，不能录制或写入个人目录")
    video_ready = True
    video_preflight = None
    if args.record_video:
        probe_env = {key: os.environ[key] for key in ("HOME", "TMPDIR", "LANG") if key in os.environ}
        if args.playwright_browsers_path:
            probe_env["PLAYWRIGHT_BROWSERS_PATH"] = str(args.playwright_browsers_path.absolute())
        package = subprocess.run([str(python), "-B", "-c",
            "import pathlib,playwright;print(pathlib.Path(playwright.__file__).parent/'driver')"],
            env=probe_env, capture_output=True, text=True, check=True).stdout.strip()
        driver_root = Path(package)
        # 只读已安装SDK的registry，不调用install，避免猜测修订号、URL或缓存布局。
        probe_js = "const r=require(process.argv[1]).registry;const f=r.registry.findExecutable('ffmpeg');console.log(JSON.stringify({revision:f.revision,registry_directory:r.registryDirectory,executable:f.executablePath(),download_urls:f.downloadURLs}));"
        video_preflight = json.loads(subprocess.run([str(driver_root / "node"), "-e", probe_js,
            str(driver_root / "package/lib/coreBundle.js")], env=probe_env,
            capture_output=True, text=True, check=True).stdout)
        executable = Path(video_preflight["executable"])
        video_ready = executable.is_file() and os.access(executable, os.X_OK)
        video_preflight.update(executable_ready=video_ready, record_dir=str(video_dir),
            model="synthetic", source="synthetic", platform="loopback", browser_started=False,
            download_performed=False, captured_surface="本地合成FastAPI UI页面；不录桌面、其他程序或RPA Chrome窗口")
        (evidence / "video-preflight.json").write_text(json.dumps(video_preflight, ensure_ascii=False, indent=2), "utf-8")
    result = {"candidate": str(root), "python": str(python), "platform": platform.system(),
        "architecture": platform.machine(), "chrome": str(chrome), "chrome_version": version,
        "driver": str(driver) if driver else None, "driver_version": driver_version,
        "browser_preflight_ready": ready, "browser_started": False,
        "planned_browser_cases": 11 if args.mode == "browser" else 0,
        "selected_browser_cases": (1 if args.first_case else len(args.cases) if args.cases else 11) if args.mode == "browser" else 0,
        "record_video": args.record_video, "video_encoder_ready": video_ready if args.record_video else None,
        "external_requests_performed": False, "dotenv_loaded": False,
        "boundary": "预检只检查本地文件和驱动版本；SE_OFFLINE不能保证阻断rpaframework下载回退"}
    (evidence / "preflight.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if args.mode == "preflight":
        return 0 if ready and video_ready else 2
    if args.mode == "browser":
        if not ready:
            print("缺少匹配的本地驱动：未启动Chrome、Robot或Selenium Manager。", file=sys.stderr)
            return 2
        if not video_ready:
            print("缺少已有Playwright ffmpeg：录屏预检停止，未启动浏览器，不自动下载编码器。", file=sys.stderr)
            return 2
    paths = [root, root / "apps/dev-agent/src", root / "services/sandbox-gateway/src",
        root / "packages/sandbox-contracts/src", root / "packages/sandbox-client/src",
        root / "packages/web-capability-tools/src", root / "packages/rpa-agent-module/src"]
    # 仅保留测试所需系统环境，固定候选导入路径，阻止editable依赖解析到原工作区。
    env = {key: os.environ[key] for key in ("HOME", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME") if key in os.environ}
    env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE="1",
        PYTHON_DOTENV_DISABLED="1", PYTHONNOUSERSITE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PATH=str(python.parent) + ":/usr/bin:/bin:/usr/sbin:/sbin", RPA_ACCEPTANCE_EVIDENCE=str(evidence),
        SE_OFFLINE="true", ROBOCORP_HOME=str(evidence / "robocorp"))
    if args.mode == "browser":
        guard = evidence / "no-download"
        guard.mkdir()
        source = (root / "packages/rpa-agent-module/tests/fixtures/no_download_sitecustomize.py").read_text("utf-8")
        # Selenium原有Service参数优先使用本地路径；只替换下载入口，真实驱动和Robot均保持原实现。
        setup = "import os\nos.environ['SE_CHROMEDRIVER'] = " + repr(str(driver)) + "\n"
        (guard / "sitecustomize.py").write_text(setup + source, "utf-8")
        env.update(RUN_RPA_ROBOT_ACCEPTANCE="1", RPA_ACCEPTANCE_GUARD_DIR=str(guard),
            PYTHONPATH=str(guard) + os.pathsep + env["PYTHONPATH"],
            PATH=str(driver.parent) + os.pathsep + env["PATH"])
        if args.record_video:
            env["RPA_ACCEPTANCE_VIDEO_ROOT"] = str(video_dir)
            if args.playwright_browsers_path:
                env["PLAYWRIGHT_BROWSERS_PATH"] = str(args.playwright_browsers_path.absolute())
    target = "packages/rpa-agent-module/tests/test_content_robot_acceptance.py"
    targets = [target]
    if args.first_case:
        targets = [target + "::test_real_ui_to_robot_success_three_times[1]"]
    elif args.cases:
        targets = [target + ("::test_real_ui_to_robot_success_three_times[" + case[-1] + "]"
            if case.startswith("normal-") else "::test_real_robot_success_then_failed_business_write_recovers_without_replay"
            if case == "writeback" else "::test_real_robot_failure_states_keep_original_job[" + case + "]")
            for case in args.cases]
    command = [str(python), "-B", "-m", "pytest", "-q", "-x", "--tb=short", "-rA",
        "--junitxml=" + str(evidence / "pytest.xml"), "-p", "no:cacheprovider",
        *targets]
    if args.mode == "offline":
        command.extend(["-k", "rejects_before_any_job or download_guard"])
    with (evidence / "pytest.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
    print((evidence / "pytest.log").read_text("utf-8"), end="")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
