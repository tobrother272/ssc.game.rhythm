import cv2
import numpy as np
import time
import argparse
import json
from moviepy.video.io.ffmpeg_writer import FFMPEG_VideoWriter
from tqdm import tqdm

def zoom_and_pan(image, points, output_file="output.mp4", fps=30, device="gpu"):
    height, width, _ = image.shape

    # Khởi tạo FFMPEG writer với codec dựa trên thiết bị
    codec = "h264_nvenc" if device == "gpu" else "libx264"
    writer = FFMPEG_VideoWriter(
        output_file, (width, height), fps=fps, codec=codec
    )

    total_start_time = time.time()

    # Zoom đến điểm đầu tiên và ghi từng khung vào video
    (x0, y0), duration0 = points[0]
    zoom_factor = 1.0

    for i in tqdm(range(int(duration0 * fps)), desc="Zooming to first point"):
        zoom_factor = 1 + (i / (duration0 * fps))
        frame = zoom_image(image, x0, y0, zoom_factor, width, height)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        writer.write_frame(frame)

    # Di chuyển giữa các điểm và ghi từng khung vào video
    for j in range(1, len(points)):
        (x1, y1), duration = points[j]
        prev_point = points[j - 1][0]

        for i in tqdm(range(int(duration * fps)), desc=f"Moving from point {j-1} to point {j}"):
            alpha = i / (duration * fps)
            x = int(prev_point[0] * (1 - alpha) + x1 * alpha)
            y = int(prev_point[1] * (1 - alpha) + y1 * alpha)

            frame = zoom_image(image, x, y, zoom_factor, width, height)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            writer.write_frame(frame)

    writer.close()
    print(f"Tổng thời gian chạy: {time.time() - total_start_time:.2f} giây")

def zoom_image(image, x, y, zoom_factor, width, height):
    new_width = int(width / zoom_factor)
    new_height = int(height / zoom_factor)

    x = max(0, min(x - new_width // 2, image.shape[1] - new_width))
    y = max(0, min(y - new_height // 2, image.shape[0] - new_height))

    cropped = image[y:y + new_height, x:x + new_width]
    return cv2.resize(cropped, (width, height))

def load_points(config_path):
    with open(config_path, 'r') as f:
        data = json.load(f)
    points = []
    for point in data["points"]:
        points.append((tuple(point["coordinates"]), point["duration"]))
    return points

def main():
    parser = argparse.ArgumentParser(description="Zoom và Pan Video Generator")
    parser.add_argument('--input_image', type=str, required=True, help='Đường dẫn đến ảnh đầu vào')
    parser.add_argument('--config', type=str, required=True, help='Đường dẫn đến file config JSON chứa các điểm')
    parser.add_argument('--output', type=str, default="output.mp4", help='Đường dẫn đến video đầu ra')
    parser.add_argument('--fps', type=int, default=30, help='Số khung hình mỗi giây')
    parser.add_argument('--device', type=str, default="gpu", choices=["cpu", "gpu"], help='Thiết bị để mã hóa video (cpu hoặc gpu)')

    args = parser.parse_args()

    image = cv2.imread(args.input_image)
    if image is None:
        raise ValueError("Không thể đọc ảnh. Hãy kiểm tra đường dẫn.")

    # Lấy kích thước của ảnh
    height, width, channels = image.shape

    # In ra kích thước
    print(f"Chiều rộng: {width} pixels")
    print(f"Chiều cao: {height} pixels")
    print(f"Số kênh: {channels}")

    points = load_points(args.config)
    if not points:
        raise ValueError("Không có điểm nào trong config.json")
    print(f"Số điểm: {len(points)}")

    # Kiểm tra các điểm có nằm trong kích thước của ảnh hay không
    for (x, y), duration in points:
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(f"Điểm ({x}, {y}) nằm ngoài kích thước của ảnh ({width}, {height})")

    zoom_and_pan(image, points, args.output, args.fps, args.device)

if __name__ == "__main__":
    main()
