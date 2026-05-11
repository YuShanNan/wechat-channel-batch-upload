"""
从视频目录生成批量上传 CSV 配置文件
"""
import csv
import sys
from pathlib import Path


def find_video_files(root_dir: str, extensions: tuple = (".mp4", ".mov", ".avi", ".mkv")) -> list:
    """递归搜索视频文件"""
    videos = []
    root = Path(root_dir)
    for ext in extensions:
        for f in root.rglob(f"*{ext}"):
            videos.append(f)
    return sorted(videos)


def generate_title(video_path: Path, base_dir: Path) -> str:
    """
    根据路径自动生成标题
    例如: E:\推文\视频号\短剧名\子目录\文件名.mp4
    标题: 短剧名 - 文件名
    """
    try:
        relative = video_path.relative_to(base_dir)
    except ValueError:
        return video_path.stem

    parts = relative.parts
    if len(parts) >= 2:
        drama_name = parts[0]
        filename = video_path.stem
        return f"{drama_name} - {filename}"
    else:
        return video_path.stem


def extract_drama_name(video_path: Path, base_dir: Path) -> str:
    """从路径中提取短剧名（第一级子目录名）"""
    try:
        relative = video_path.relative_to(base_dir)
        parts = relative.parts
        if len(parts) >= 2:
            return parts[0]
    except ValueError:
        pass
    return ""


def generate_csv(
    video_dir: str,
    output_path: str,
    extensions: tuple = (".mp4",),
    default_publish_time: str = "",
):
    """
    从视频目录生成 CSV

    CSV 列:
        video_path, title, description, short_drama_name,
        publish_time, cover_path, location
    """
    base_dir = Path(video_dir).resolve()
    videos = find_video_files(video_dir, extensions)

    if not videos:
        print(f"在 {video_dir} 中未找到视频文件")
        return

    rows = []
    for video in videos:
        drama_name = extract_drama_name(video, base_dir)
        title = generate_title(video, base_dir)
        description = f"#{drama_name} #短剧" if drama_name else ""

        rows.append({
            "video_path": str(video),
            "title": title,
            "description": description,
            "short_drama_name": drama_name,
            "publish_time": default_publish_time,
            "cover_path": "",  # 用户手动填或自动匹配
            "location": "none",
        })

    # 写入 CSV (UTF-8 with BOM, Excel 友好)
    fieldnames = [
        "video_path", "title", "description", "short_drama_name",
        "publish_time", "cover_path", "location",
    ]
    output = Path(output_path)
    with open(output, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"已生成 {len(rows)} 条记录 → {output}")
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python csv_generator.py <视频目录> [输出CSV路径]")
        print("示例: python csv_generator.py E:\\推文\\视频号 batch-config.csv")
        sys.exit(1)

    video_dir = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else "batch-config.csv"
    generate_csv(video_dir, output)
