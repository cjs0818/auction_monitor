from landwatch.runner import run_once

if __name__ == "__main__":
    result = run_once("config/config.yaml", notify=True)
    print(f"found={len(result.items)} new={len(result.new_items)} changed={len(result.changed_items)}")
