# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""抓取 gitgud (GitLab) 项目数据，生成 data/gitgud.json 供 era-game shortcode 使用。

设计要点：
  A. 增量合并：以磁盘上已有的 data/gitgud.json 为基线（CI 中由 actions/cache 还原），
     抓取成功的条目覆盖旧值，抓取失败（524/超时/网络错误）的条目保留旧值。
  C. 完整性门槛：输出 data/fetch-report.json + reports/fetch-report.md（后者不能放 data/，
     Hugo 会把 data/ 下所有文件当数据文件解析）；只要存在
     "既没抓到、也没有旧数据"的条目，就以非 0 退出，让 workflow 失败、不部署残缺站点。
  E. 降低单请求开销：项目接口带 simple=true（跳过 permissions/license/statistics 等
     服务端计算）。实测本项目这些公开项目响应体无变化，保留以备上游调整。
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CONTENT_DIR = "content"
DATA_DIR = "data"
OUTPUT_FILE = os.path.join(DATA_DIR, "gitgud.json")
REPORT_JSON = os.path.join(DATA_DIR, "fetch-report.json")
# Hugo 会把 data/ 下的每个文件都当作结构化数据加载，data/ 里出现 .md 会让整个站点
# 构建直接失败（unmarshal of format "" is not supported），所以可读报告写到 data/ 之外。
REPORT_DIR = "reports"
REPORT_MD = os.path.join(REPORT_DIR, "fetch-report.md")
GITGUD_API_BASE = "https://gitgud.io/api/v4/projects"
ACCESS_TOKEN = os.getenv("HUGO_GITGUD_ACCESS_TOKEN")
IS_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"

# 添加浏览器 User-Agent 以绕过 Cloudflare 的 bot 检测（Error 1010）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# {{< era-game "repo_id" >}} / {{< era-game "repo_id" "branch" >}} / 第三个参数是下载链接
SHORTCODE_RE = re.compile(r'\{\{<\s*era-game\s+"([^"]+)"(?:\s+"([^"]+)")?.*?>\}\}')
# 被 HTML 注释包起来的 shortcode 不会渲染到页面，也不该去抓取
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

# 抓取结果状态
FRESH = "fresh"      # 本次抓取成功
STALE = "stale"      # 本次失败，沿用上一次的数据
GONE = "gone"        # 上游 404：项目/分支已不存在（内容问题，不阻塞发布，但要告警）
MISSING = "missing"  # 本次失败且没有旧数据可用（阻塞发布）


def fetch_json(url):
    """GET 一个 JSON 接口。

    返回 (data, status)：解析成功时 data 为对象，否则 None；status 为 HTTP 状态码，
    网络层错误（超时/连接失败）时为 None。
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    if ACCESS_TOKEN:
        req.add_header("PRIVATE-TOKEN", ACCESS_TOKEN)

    max_retries = 5
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode()), response.status
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"Rate limited. Waiting to retry... ({attempt + 1}/{max_retries})")
                time.sleep(2 ** attempt)
                continue
            print(f"HTTP Error {e.code}: {url}")
            print(f"--- {e.reason}")
            # 尝试读取响应体获取详细报错信息
            try:
                error_body = e.read().decode()
                print(error_body[:500])  # 只打印前 500 字符，避免 Cloudflare HTML 页面刷屏
            except Exception:
                pass
            return None, e.code
        except Exception as e:
            print(f"Error fetching {url}: {e}")
            return None, None
    return None, None


def load_cached():
    """读取上一次构建留下的 data/gitgud.json（CI 中由 actions/cache 还原）。"""
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            print(f"Loaded cached data for {len(data)} repos from {OUTPUT_FILE}")
            return data
        print(f"Cache file {OUTPUT_FILE} is empty; starting from scratch.")
    except FileNotFoundError:
        print(f"No cache at {OUTPUT_FILE}; starting from scratch.")
    except Exception as e:
        print(f"WARNING: cache unreadable ({e}); ignoring it.")
    return {}


def collect_repos():
    """扫描 content/**/*.md，收集 (repo_id, branch) 需求。"""
    repos = {}
    if not os.path.isdir(CONTENT_DIR):
        print(f"ERROR: content dir {CONTENT_DIR!r} not found")
        sys.exit(1)
    for root, _, files in os.walk(CONTENT_DIR):
        for file in files:
            if not file.endswith(".md"):
                continue
            file_path = os.path.join(root, file)
            with open(file_path, "r", encoding="utf-8") as f:
                content = HTML_COMMENT_RE.sub("", f.read())
            for match in SHORTCODE_RE.findall(content):
                repo_id = match[0]
                branch = match[1] if match[1] else "master"
                info = repos.setdefault(
                    repo_id,
                    {"branches": [], "encoded_id": urllib.parse.quote(repo_id, safe="")},
                )
                if branch not in info["branches"]:
                    info["branches"].append(branch)
    return repos


def classify_failure(status_code, has_cache):
    """把失败归类：404 是"上游没了"，属于内容问题；其余按临时故障处理。"""
    if status_code == 404:
        return GONE
    return STALE if has_cache else MISSING


def fetch_repo(repo_id, info, old):
    """抓一个仓库的元信息与各分支提交。返回 (repo_info, sources, reasons)。"""
    encoded_id = info["encoded_id"]
    old_branches = old.get("branches") or {}
    repo_info = {
        "name": old.get("name", ""),
        "description": old.get("description", ""),
        "branches": {b: list(c) for b, c in old_branches.items()},
    }
    sources = {"meta": FRESH, "branches": {}}
    reasons = {}

    # ?simple=true: 让服务端跳过 permissions/license/statistics 等计算
    repo_data, meta_code = fetch_json(f"{GITGUD_API_BASE}/{encoded_id}?simple=true")
    if repo_data:
        repo_info["name"] = repo_data.get("name", "")
        repo_info["description"] = repo_data.get("description", "")
    else:
        sources["meta"] = classify_failure(meta_code, bool(old.get("name")))
        reasons["meta"] = f"HTTP {meta_code}" if meta_code else "network error"

    for branch in info["branches"]:
        print(f"  Fetching commits for branch {branch}...")
        encoded_branch = urllib.parse.quote(branch, safe="")
        commits_data, code = fetch_json(
            f"{GITGUD_API_BASE}/{encoded_id}/repository/commits?ref_name={encoded_branch}"
        )
        if commits_data:
            # 只需要前几条提交用于页面展示
            repo_info["branches"][branch] = [
                {
                    "message": c.get("message", ""),
                    "web_url": c.get("web_url", ""),
                    "committed_date": c.get("committed_date", ""),
                }
                for c in commits_data[:3]
            ]
            sources["branches"][branch] = FRESH
        else:
            state = classify_failure(code, branch in old_branches)
            sources["branches"][branch] = state
            reasons[branch] = f"HTTP {code}" if code else "network error"

    return repo_info, sources, reasons


def worst(statuses):
    """取一组状态里最严重的一个，用于整仓汇总。"""
    order = [FRESH, STALE, GONE, MISSING]
    return max(statuses, key=order.index) if statuses else FRESH


def write_report(report):
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    counts = report["counts"]
    lines = [
        "## Gitgud 数据抓取报告",
        "",
        f"- 时间：{report['generated_at']}",
        f"- 仓库 {counts['total']} 个：本次成功 **{counts['fresh']}**，"
        f"沿用旧数据 **{counts['stale']}**，上游已删除 **{counts['gone']}**，"
        f"完全缺失 **{counts['missing']}**",
    ]
    if counts["missing"]:
        lines.append("- ❌ 有仓库既没抓到、也没有旧数据，本次构建判定失败，不会部署。")
    elif counts["stale"] or counts["gone"]:
        lines.append("- ⚠️ 站点已发布，但部分卡片的数据来自上一次成功抓取。")
    else:
        lines.append("- ✅ 全部数据为本次实时抓取。")

    problem_rows = []
    for repo_id, entry in report["repos"].items():
        parts = [("项目信息", "meta", entry["meta"])]
        parts += [(b, b, s) for b, s in entry["branches"].items()]
        for label, key, state in parts:
            if state == FRESH:
                continue
            problem_rows.append(
                (repo_id, entry["name"], label, state, entry["reasons"].get(key, ""))
            )

    if problem_rows:
        lines += ["", "| 仓库 | 名称 | 对象 | 状态 | 原因 |", "| --- | --- | --- | --- | --- |"]
        for repo_id, name, part, state, reason in problem_rows:
            label = {STALE: "沿用旧数据", GONE: "上游 404", MISSING: "缺失"}[state]
            lines.append(f"| {repo_id} | {name or '-'} | {part} | {label} | {reason} |")

    md = "\n".join(lines) + "\n"
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(md)

    print(md)
    if IS_ACTIONS:
        for repo_id, name, part, state, reason in problem_rows:
            who = f"仓库 {repo_id}（{name or '未知'}）{part}"
            if state == MISSING:
                print(f"::error::{who} 抓取失败且无旧数据：{reason}")
            elif state == GONE:
                print(f"::warning::{who} 上游已删除（{reason}），请清理 content 中对应的 era-game shortcode")
            else:
                print(f"::warning::{who} 抓取失败（{reason}），已沿用上次成功的数据")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    repos = collect_repos()
    print(f"Found {len(repos)} repos in {CONTENT_DIR}")
    cached = load_cached()

    data = {}
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repos": {},
        "counts": {"total": 0, "fresh": 0, "stale": 0, "gone": 0, "missing": 0},
    }

    for repo_id, info in repos.items():
        print(f"Fetching data for {repo_id}...")
        old = cached.get(repo_id) or {}
        repo_info, sources, reasons = fetch_repo(repo_id, info, old)

        state = worst([sources["meta"]] + list(sources["branches"].values()))
        report["counts"][state] += 1
        report["counts"]["total"] += 1
        report["repos"][repo_id] = {
            "name": repo_info["name"],
            "meta": sources["meta"],
            "branches": sources["branches"],
            "reasons": reasons,
        }

        # 没有任何可用内容就不写入，避免污染数据
        if not repo_info["name"] and not repo_info["branches"]:
            continue

        data[repo_id] = repo_info
        time.sleep(1)  # Avoid hitting limits too quickly

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(data)} repos to {OUTPUT_FILE}")

    write_report(report)

    counts = report["counts"]
    print(
        f"Data fetch complete: {counts['fresh']} fresh, {counts['stale']} stale, "
        f"{counts['gone']} gone, {counts['missing']} missing."
    )
    if counts["missing"]:
        print("ERROR: 有仓库既没抓到也没有旧数据，构建失败以防发布残缺站点。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
