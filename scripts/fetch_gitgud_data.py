import os
import re
import json
import urllib.request
import urllib.error
import time

CONTENT_DIR = "content"
OUTPUT_FILE = "data/gitgud.json"
GITGUD_API_BASE = "https://gitgud.io/api/v4/projects"
ACCESS_TOKEN = os.getenv("HUGO_GITGUD_ACCESS_TOKEN")

def fetch_json(url):
    req = urllib.request.Request(url)
    if ACCESS_TOKEN:
        req.add_header("PRIVATE-TOKEN", ACCESS_TOKEN)
    
    max_retries = 5
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"Rate limited. Waiting to retry... ({attempt + 1}/{max_retries})")
                time.sleep(2 ** attempt)
            else:
                print(f"HTTP Error {e.code}: {url}")
                return None
        except Exception as e:
            print(f"Error fetching {url}: {e}")
            return None
    return None

def main():
    if not os.path.exists("data"):
        os.makedirs("data")

    # regex to find {{< era-game "repo_id" "branch" ... >}}
    # Hugo shortcodes can be positional or named, but looks like positional in this project based on:
    # {{ $GameRepoId := .Get 0 }}
    # {{ $Branch     := .Get 1 | default "master" }}
    
    # Let's match: {{< era-game "repo_id" >}} or {{< era-game "repo_id" "branch" >}}
    pattern = re.compile(r'\{\{<\s*era-game\s+"([^"]+)"(?:\s+"([^"]+)")?.*?>\}\}')
    
    repos = {}

    for root, _, files in os.walk(CONTENT_DIR):
        for file in files:
            if file.endswith(".md"):
                file_path = os.path.join(root, file)
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    matches = pattern.findall(content)
                    for match in matches:
                        repo_id = match[0]
                        branch = match[1] if match[1] else "master"
                        
                        # Fix encoding for repo_id which might contain slashes
                        encoded_repo_id = urllib.parse.quote(repo_id, safe='')
                        
                        if repo_id not in repos:
                            repos[repo_id] = {"branches": set()}
                        repos[repo_id]["branches"].add(branch)
                        repos[repo_id]["encoded_id"] = encoded_repo_id

    data = {}
    
    for repo_id, info in repos.items():
        print(f"Fetching data for {repo_id}...")
        encoded_id = info["encoded_id"]
        repo_data = fetch_json(f"{GITGUD_API_BASE}/{encoded_id}")
        if not repo_data:
            continue
            
        repo_info = {
            "name": repo_data.get("name", ""),
            "description": repo_data.get("description", ""),
            "branches": {}
        }
        
        for branch in info["branches"]:
            print(f"  Fetching commits for branch {branch}...")
            encoded_branch = urllib.parse.quote(branch, safe='')
            commits_data = fetch_json(f"{GITGUD_API_BASE}/{encoded_id}/repository/commits?ref_name={encoded_branch}")
            
            if commits_data:
                # We only need the first few commits (up to 3 for the UI)
                commits = []
                for commit in commits_data[:3]:
                    commits.append({
                        "message": commit.get("message", ""),
                        "web_url": commit.get("web_url", ""),
                        "committed_date": commit.get("committed_date", "")
                    })
                repo_info["branches"][branch] = commits
        
        # Replace slashes and other characters for Hugo key compatibility
        safe_repo_key = repo_id
        data[safe_repo_key] = repo_info
        time.sleep(1) # Avoid hitting limits too quickly

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("Data fetch complete.")

if __name__ == "__main__":
    import urllib.parse
    main()
