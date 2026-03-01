#!/usr/bin/env python3
import argparse, base64, hashlib, json, os, re, subprocess, sys, textwrap, time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
WORKFLOW_DIR = os.path.join(REPO_ROOT, ".github", "workflows")
CONFIG_FILE = os.path.join(SCRIPT_DIR, ".repay-config.json")
POLL_INTERVAL = 10
MAX_WAIT = 600

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"[repay] config saved to {CONFIG_FILE}")

def get_config(key, required=False):
    cfg = load_config()
    val = cfg.get(key)
    if required and not val:
        print(f"[repay] ERROR: '{key}' not configured.", file=sys.stderr)
        sys.exit(1)
    return val

def get_repo():
    cfg = load_config()
    if cfg.get("repo"):
        return cfg["repo"]
    try:
        out = subprocess.run("git remote get-url origin", shell=True,
                             capture_output=True, text=True, timeout=5, cwd=REPO_ROOT)
        url = out.stdout.strip()
        m = re.search(r'[/:]([^/:]+/[^/.]+?)(?:\.git)?$', url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None

def run(cmd, check=True, capture=True, timeout=60):
    r = subprocess.run(cmd, shell=True, capture_output=capture, text=True,
                       timeout=timeout, cwd=REPO_ROOT)
    if check and r.returncode != 0:
        print(f"ERROR: {cmd}", file=sys.stderr)
        if r.stderr:
            print(r.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    return r.stdout.strip() if capture else ""

def get_branch():
    return run("git branch --show-current")

def gen_id(name=None, cmd=None):
    if name:
        clean = re.sub(r'[^a-zA-Z0-9_-]', '', name)[:32]
        return clean or gen_id(cmd=cmd or "task")
    seed = (cmd or "") + str(time.time())
    return hashlib.md5(seed.encode()).hexdigest()[:8]

def git_push_retry(branch, max_retries=4):
    for attempt in range(max_retries):
        result = subprocess.run(f"git push -u origin {branch}", shell=True,
                                capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
        if result.returncode == 0:
            return True
        if attempt < max_retries - 1:
            wait = 2 ** (attempt + 1)
            print(f"  push failed, retrying in {wait}s...")
            time.sleep(wait)
    print(f"ERROR: push failed after {max_retries} attempts", file=sys.stderr)
    return False

def progress_bar(elapsed, max_time, width=30, phase=""):
    ratio = min(elapsed / max_time, 1.0)
    filled = int(width * ratio)
    bar = "#" * filled + "." * (width - filled)
    spinners = ["|", "/", "-", "\\"]
    spinner = spinners[int(elapsed) % len(spinners)]
    if not phase:
        phases = [(10, "pushing..."), (20, "queuing..."), (40, "provisioning..."),
                  (60, "waking up..."), (90, "installing..."), (120, "executing..."),
                  (180, "almost..."), (300, "working..."), (9999, "patience...")]
        phase = next(p for t, p in phases if elapsed < t)
    pct = int(ratio * 100)
    sys.stdout.write(f"\r  {spinner} [{bar}] {pct}% ({int(elapsed)}s) {phase}".ljust(78))
    sys.stdout.flush()

def create_runner_workflow(task_id, script_content, script_ext=".sh"):
    run_cmd = f"python3 payload{script_ext}" if script_ext == ".py" else f"bash payload{script_ext}"
    encoded = base64.b64encode(script_content.encode()).decode()
    branch = get_branch()
    workflow = f"""name: "repay: {task_id}"
on:
  push:
    branches: ['{branch}']
    paths: ['.github/workflows/repay-{task_id}.yml']
jobs:
  execute:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
        with:
          ref: '{branch}'
      - name: Install tools
        run: sudo apt-get update -qq && sudo apt-get install -y -qq jq whois dnsutils lynx > /dev/null 2>&1
      - name: Execute
        run: |
          echo '{encoded}' | base64 -d > payload{script_ext}
          mkdir -p results
          echo "task: {task_id}" > results/meta.txt
          echo "started: $(date -u)" >> results/meta.txt
          {run_cmd} > results/stdout.txt 2> results/stderr.txt || true
          echo "finished: $(date -u)" >> results/meta.txt
          echo "exit_code: $?" >> results/meta.txt
      - name: Commit results
        run: |
          git config user.name "repay[bot]"
          git config user.email "repay[bot]@users.noreply.github.com"
          git add results/
          git commit -m "repay: {task_id} done" || true
          git push origin {branch}
      - name: Cleanup
        run: |
          rm -f .github/workflows/repay-{task_id}.yml
          git add .github/workflows/
          git commit -m "repay: cleanup {task_id}" || true
          git push origin {branch}
"""
    path = os.path.join(WORKFLOW_DIR, f"repay-{task_id}.yml")
    os.makedirs(WORKFLOW_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(workflow)
    return path

def oneshot(task_id, script_content, script_ext=".sh", wait=True, vps=False):
    target = "VPS" if vps else "runner"
    print(f"[repay] task: {task_id}")
    print(f"[repay] target: {target} {'(wait)' if wait else '(fire & forget)'}")

    result_subdir = os.path.join("results", "vps") if vps else "results"
    result_dir = os.path.join(REPO_ROOT, result_subdir)
    os.makedirs(result_dir, exist_ok=True)

    for fname in ["stdout.txt", "stderr.txt", "meta.txt"]:
        fpath = os.path.join(result_dir, fname)
        if os.path.exists(fpath):
            with open(fpath, "w") as f:
                f.write("")

    wf_path = create_runner_workflow(task_id, script_content, script_ext)
    rel_path = os.path.relpath(wf_path, REPO_ROOT)

    run(f"git add {rel_path}")
    run(f"git add {result_subdir}/")
    prefix = "repay"
    run(f'git commit -m "{prefix}: launch {task_id}"')

    branch = get_branch()
    if not git_push_retry(branch):
        sys.exit(1)

    print(f"[repay] pushed -> {target}")

    if not wait:
        print(f"[repay] fire & forget:")
        print(f"  git pull && cat {result_subdir}/stdout.txt")
        return

    print(f"[repay] waiting...\n")
    start = time.time()
    while time.time() - start < MAX_WAIT:
        progress_bar(time.time() - start, MAX_WAIT)
        time.sleep(POLL_INTERVAL)
        progress_bar(time.time() - start, MAX_WAIT, phase="pulling...")
        subprocess.run(f"git pull origin {branch} --rebase 2>/dev/null",
                       shell=True, capture_output=True, text=True, timeout=15, cwd=REPO_ROOT)
        result_file = os.path.join(result_dir, "stdout.txt")
        if os.path.exists(result_file) and os.path.getsize(result_file) > 1:
            sys.stdout.write(f"\r  OK [{'#' * 30}] 100% ({int(time.time() - start)}s) done!".ljust(78))
            sys.stdout.write("\n\n")
            print("-" * 60)
            with open(result_file) as f:
                print(f.read())
            print("-" * 60)
            stderr_file = os.path.join(result_dir, "stderr.txt")
            if os.path.exists(stderr_file):
                with open(stderr_file) as f:
                    err = f.read().strip()
                if err:
                    print(f"[stderr]\n{err}")
            return

    sys.stdout.write(f"\r  TIMEOUT [{' ' * 30}] ({MAX_WAIT}s)".ljust(78) + "\n")
    print(f"\n[repay] Check manually: git pull && cat {result_subdir}/stdout.txt")

def show_last():
    for label, subdir in [("VPS", "results/vps"), ("Runner", "results")]:
        fpath = os.path.join(REPO_ROOT, subdir, "stdout.txt")
        if os.path.exists(fpath) and os.path.getsize(fpath) > 1:
            print(f"[repay] Last {label} result:")
            print("-" * 60)
            with open(fpath) as f:
                print(f.read())
            print("-" * 60)
            return
    print("[repay] No results found")

def show_help():
    print("repay-lite -- Remote Execute, Push And Yield")

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "help":
        show_help()
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?")
    parser.add_argument("-f", "--file")
    parser.add_argument("-n", "--name")
    parser.add_argument("-v", "--vps", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--last", action="store_true")
    args = parser.parse_args()
    if args.last:
        show_last()
        return
    if args.file:
        with open(args.file) as f:
            content = f.read()
        ext = os.path.splitext(args.file)[1] or ".sh"
    elif args.command:
        content = args.command
        ext = ".sh"
    else:
        parser.error("Need command or --file")
        return
    task_id = gen_id(name=args.name, cmd=args.command)
    oneshot(task_id, content, ext, wait=not args.no_wait, vps=args.vps)

if __name__ == "__main__":
    main()
