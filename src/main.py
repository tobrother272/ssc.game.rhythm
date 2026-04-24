import sys
from audio_processing import *
from video_generation import *
from image_processing import *
from utils import get_bar_color
from pydub import AudioSegment
from authorization import *
import cv2
import subprocess
import numpy as np
import os
import argparse
import time
import uuid
import concurrent.futures
import queue

mean_range = {
    "bars": 2,
    "disk_bars": 2,
    "circular_bars": 1,
    "up_bars": 2,
    "bars_with_cap": 2,
    "line": 2,
    "tiles": 2,
    "revert_tiles": 2,
    "tiles_with_cap": 2,
    "revert_tiles_with_cap": 2,
    "liquid": 1,
    "double_liquid": 1,
    "circular_liquid": 1,
    "signal": 2,
    "circular_signal": 2,
    "string": 10,
    "oval": 2,
    "image": 2,
    "sharp_circular_liquid": 1,
    "symmetric_bars": 2,
}

max_scale = {
    "bars": 1.8,
    "disk_bars": 1.8,
    "circular_bars": 1.6,
    "up_bars": 1.7,
    "bars_with_cap": 2.1,
    "line": 1.6,
    "tiles": 1.6,
    "tiles_with_cap": 1.6,
    "revert_tiles": 1.6,
    "revert_tiles_with_cap": 1.6,
    "liquid": 1.7,
    "double_liquid": 1.7,
    "circular_liquid": 1.5,
    "signal": 1.6,
    "circular_signal": 1.7,
    "oval": 1.6,
    "string": 2,
    "image": 1.5,
    "sharp_circular_liquid": 1.5,
    "symmetric_bars": 1.5,
}

n_bars = {
    "bars": 120,
    "disk_bars": 120,
    "circular_bars": 60,
    "up_bars": 120,
    "bars_with_cap": 120,
    "line": 90,
    "tiles": 60,
    "tiles_with_cap": 60,
    "revert_tiles": 60,
    "revert_tiles_with_cap": 60,
    "signal": 120,
    "circular_signal": 150,
    "oval": 60,
    "string": 30,
    "liquid": 120,
    "double_liquid": 120,
    "circular_liquid": 240,
    "image": 80,    
    "sharp_circular_liquid": 150,
    "symmetric_bars": 120,
}
        
def main(args):
    # Bắt đầu xử lý loại hình ảnh hóa được chỉ định
    print(f"----Begin processing {args.type}-----")
    
    # Xác thực người dùng nếu có token
    if args.token:
        isAuth = authourize_user(args.token, args.url)
        if not isAuth:
            sys.exit(1)
    else:
        print("Failed to authenticate the user. Please provide a valid token.")
        sys.exit(1)

    # Tải tệp âm thanh
    audio = AudioSegment.from_file(args.input)
    print("Audio file loaded successfully")
    
    # Cắt âm thanh để xử lý nhanh hơn
    audio = audio[:int(len(audio) / 50)]
    fps = 25
    width, height = args.width, args.height
    samples_per_frame = audio.frame_rate // fps
    sample_rate = audio.frame_rate
    window = np.hanning(samples_per_frame)
    background_frames = None

    # Tính toán tổng số khung hình
    total_frames = int(len(audio) / 1000 * fps)
    n_bar = n_bars[args.type]
    
    # Điều chỉnh số lượng thanh dựa trên loại hình ảnh hóa
    if args.type in ["bars", "up_bars", "bars_with_cap", "oval", "disk_bars"]:
        n_bar = int(args.width // (args.size * 5 // 3))
    if args.type in ["tiles", "revert_tiles", "tiles_with_cap", "revert_tiles_with_cap",]:
        n_bar = int(args.width // (args.size + 4))
    elif args.type in ["circular_bars"]:
        n_bar = int(width // 3 // int(args.size + 4))
    elif args.type in ["symmetric_bars"]:
        n_bar = int(width // 3 // int(args.size + 4))

    # Điều chỉnh tỷ lệ tối đa cho hình ảnh nếu chế độ là 'bass'
    if args.type == 'image' and args.mode == 'bass':
        max_scale['image'] = 1.3
    spectrums = []
    global_max = 0
    option = {
        'size': int(args.size),
        'glow': int(args.glow),
    }

    # Tải nền nếu có
    if hasattr(args, 'background'):
        if args.background is not None:
            background_frames = []
            file_extension = args.background.split('.')[-1]
            if file_extension == "mp4":
                print("Loading background video...")
                i = 0
                background_video_path = args.background
                background_cap = cv2.VideoCapture(background_video_path)
                background_fps = background_cap.get(cv2.CAP_PROP_FPS)
                while True:
                    ret, frame = background_cap.read()
                    if not ret:
                        break
                    background_frames.append(cv2.resize(frame, (width, height)))
                    print(f"Loaded {i} frames", end='\r')
                    i += 1
            else:
                print("Loading background image...")
                background_image = cv2.imread(args.background)
                background_image = cv2.resize(background_image, (width, height))
                background_frames = [background_image]
                background_fps = 24
            
            fps_ratio = background_fps / fps
            num_background_frames = len(background_frames)

    # Kiểm tra xem có xuất ra video hay không
    to_video = False
    if args.type == 'image' or args.to_video == 1:
        to_video = True

    # Thiết lập đầu ra video hoặc thư mục hình ảnh
    if to_video:
        if (args.audio == 1): 
            temp_file = "./temp/temp_video/" + str(uuid.uuid4()) + ".mp4"
            if not os.path.isdir("./temp/temp_video/"):
                os.mkdir("./temp/temp_video/")
        else: temp_file = args.output + ".mp4"
        video = cv2.VideoWriter(f"{temp_file}", cv2.VideoWriter_fourcc(*'mp4v'), fps, (width, height))
    else:
        image_folder = args.output
        if not os.path.isdir(image_folder):
            os.makedirs(image_folder)

    # Bắt đầu xử lý âm thanh
    print("Start processing...")
    for frame_num in range(total_frames):
        start_ms = int(frame_num * (1000 / fps))
        end_ms = start_ms + int((1000 / fps))

        segment = np.array(audio[start_ms:end_ms].get_array_of_samples())
        if audio.channels == 2:
            segment = segment[::2]

        if len(segment) < samples_per_frame:
            padding = np.zeros(samples_per_frame - len(segment))
            segment = np.concatenate((segment, padding))

        spectrum = calculate_spectrum(segment, n_bar, window, mean_range[args.type], sample_rate)
        spectrum = np.log1p(spectrum)  # Nén logarit để giảm sự khác biệt lớn giữa các thanh
        if args.type == 'image' and args.mode == 'treble':
            spectrums = spectrums + [spectrum[38:40]]
            global_max = max(global_max, np.max(spectrum[38:40]))
        else:
            spectrums = spectrums + [spectrum]
            global_max = max(global_max, np.max(spectrum))

        progress = (frame_num + 1) / total_frames * 100
        print(f"Audio Processing: {progress:.2f}% complete", end='\r')
    
    print()

    # Khởi tạo các biến cho quá trình vẽ
    prev_spectrum = np.zeros_like(spectrums[0])
    global_max *= max_scale[args.type]

    circular_img = None
    if 'circular' in args.type and args.image_path:
        radius = min(height, width) // 3  # Đặt bán kính phù hợp dựa trên yêu cầu. Bán kính phù hợp là 1/3 chiều cao hoặc chiều rộng của khung hình
        circular_img = load_and_crop_image(args.image_path, radius)
    if args.type in ["tiles_with_cap", "revert_tiles_with_cap"]:
        option['prev_max'] = np.full(n_bar, height * 3 // 2)
        option['cap_color'] = args.cap_color
    if args.type in ["string"]:
        option["peak"] = 9  
        option["move"] = 1
    if "tile" in args.type:
        option["max_tiles"] = height // int(args.tile_height)
    if args.type == "image": 
        image = cv2.imread(args.image_path, cv2.IMREAD_COLOR)
        if args.fit == "fit":
            option['image'] = fit_image(image, (width, height))
        # nếu args.fit là một số nguyên. args.fit là một số nguyên thì zoom ảnh lên
        elif args.fit.isdigit():
            option['image'] = zoom_image(image, int(args.fit) / 100.0)
        elif args.fit == "origin":
            option['image'] = image
        else:
            fit = args.fit.split(',')
            ix = int(fit[0])
            iy = int(fit[1])
            iw = int(fit[2])
            ih = int(fit[3])
            option['image'] = fit_image(image, (iw, ih))
            option['x'] = ix
            option['y'] = iy
        option['mode'] = args.mode
        if hasattr(args, 'scale'):
            option['scale'] = args.scale / 100
        else: 
            option['scale'] = 0.3
    if args.type == "disk_bars":
        disk_size = int(args.disk_size)
        option['image'] = load_and_crop_image(args.disk_path, disk_size)
        option['radius'] = disk_size // 2
    if args.type == "symmetric_bars":
        image = cv2.imread(args.image_path, cv2.IMREAD_COLOR)
        option['image'] = fit_image(image, (width, height))
        option['bar_height'] = int(args.bar_height)
    if to_video: 
        option['mp4'] = 1
    else: 
        option['mp4'] = 0

    # Bắt đầu quá trình vẽ và tạo video/hình ảnh
    start_time = time.time()
    progress_queue = queue.Queue()

    def process_frame(frame_num, prev_spectrum):
        video_frame = np.zeros((height, width, 4), dtype=np.uint8)

        # Thêm nền nếu có
        if background_frames is not None:
            background_frame_index = int((frame_num / fps_ratio) % num_background_frames)
            background_frame = background_frames[background_frame_index]
            alpha_mask = np.ones(background_frame.shape[:2], dtype=np.uint8)
            background_frame_rgb = background_frame[:, :, :3]
            alpha_channel = np.full((height, width, 1), 255, dtype=np.uint8)
            background_frame_rgba = np.concatenate((background_frame_rgb, alpha_channel), axis=2)
            overlay_image_alpha(video_frame, background_frame_rgba, 0, 0)
            
        # Tính toán phổ và vẽ dạng sóng
        spectrum = np.interp(spectrums[frame_num], (0, global_max), (0, 1))
        spectrum = sigmoid(spectrum)
        spectrum = smooth_transition(prev_spectrum, spectrum)
        prev_spectrum = spectrum

        option['frame_number'] = frame_num
        draw_waveform(video_frame, spectrum, n_bar, args.color, args.type, option)

        # Điều chỉnh các tham số cho loại 'string'
        if args.type == 'string':
            if frame_num % 96 == 0:
                option['move'] = -option['move']
            option['peak'] += option['move'] * (6 // abs(15 - option['peak']))
            option['peak'] = min(option['peak'], 21)
            option['peak'] = max(option['peak'], 9)
        
        # Thêm hình ảnh tròn nếu có
        if 'circular' in args.type and circular_img is not None:           
            rotation_angle = - frame_num * 360 / total_frames  # Góc xoay
            rotated_circular_img = rotate_image(circular_img, rotation_angle)
            # Đảm bảo hình ảnh tròn có kênh alpha
            if rotated_circular_img.shape[2] == 4:
                alpha_mask = rotated_circular_img[:, :, 3] / 255.0  # Chuẩn hóa kênh alpha
                circular_img_rgb = rotated_circular_img[:, :, :4]   # Kênh RGB

                center = (width // 2, height // 2)
                overlay_image_alpha(video_frame, circular_img_rgb, center[0] - rotated_circular_img.shape[1] // 2, center[1] - rotated_circular_img.shape[0] // 2)

        # Lật video nếu cần
        if args.flip == '1':
            video_frame = cv2.flip(video_frame, 0)  
        
        # Ghi khung hình vào video hoặc lưu dưới dạng hình ảnh
        if to_video:
            video.write(video_frame[:, :, :3])
        else:
            cv2.imwrite(f"{image_folder}/frame_{frame_num:06d}.png", video_frame)
        
        progress = (frame_num + 1) / total_frames * 100
        progress_queue.put(f"Frames Rendering: {progress:.2f}% complete")

    print("Starting frame processing...")
    # with concurrent.futures.ProcessPoolExecutor(max_workers=3) as executor:  # Giảm số lượng max_workers
    #     futures = [executor.submit(process_frame, frame_num) for frame_num in range(total_frames)]
    #     for future in concurrent.futures.as_completed(futures):
    #         print(progress_queue.get(), end='\r')

    for frame_num in range(total_frames):
        process_frame(frame_num, prev_spectrum)
        print(progress_queue.get(), end='\r')
    print("Done")

    # Hoàn tất quá trình tạo video
    if to_video:
        video.release()
        if args.audio == 1:
            subprocess.run(['ffmpeg', '-y', '-i', temp_file, '-i', args.input, '-c:v', 'h264_nvenc', '-pix_fmt', 'yuv420p', '-vsync', '2', '-c:a', 'aac', '-strict', 'experimental', f"{args.output}.mp4"])
            os.remove(temp_file)

    # In thời gian xử lý và kết thúc
    print(f"Rendering time: {time.time() - start_time:.2f} seconds")
    print(f"----Finish processing {args.type}----")
    print()

if __name__ == "__main__":
    # Thiết lập các tham số đầu vào
    parser = argparse.ArgumentParser(description='Audio Visualization Tool')
    parser.add_argument('-W', '--width', required=True, type=int, help='Width of the video')
    parser.add_argument('-H', '--height', required=True, type=int, help='Height of the video')
    parser.add_argument('-i', '--input', required=True, help='Input audio file path')
    parser.add_argument('-o', '--output', required=True, help='Output folder path')
    parser.add_argument('-c', '--color', help='Color scheme for visualization')
    parser.add_argument('-s', '--size', required=True, type=float, help='Size of the elements')
    parser.add_argument('-a', '--audio', required=False, type=int, help='Add audio to the video')
    parser.add_argument('-t', '--token', required=True, help='Authentication token for API access')
    parser.add_argument('-u', '--url', required=True, help='Authentication URL for API access')
    parser.add_argument('-g', '--glow', required=False, help='Glow strength, default is 60')
    parser.add_argument('-f', '--flip', required=True, help='Flip the video vertically')
    parser.add_argument('--to_video', required=False, type=int, help='The output is video or not (default is image)')

    # Thiết lập các loại hình ảnh hóa
    subparsers = parser.add_subparsers(dest='type', required=True, help='Type of visualization')

    parser_bars = subparsers.add_parser('bars')
    parser_up_bars = subparsers.add_parser('up_bars')
    parser_bars = subparsers.add_parser('bars_with_cap')
    parser_line= subparsers.add_parser('line')
    parser_signal = subparsers.add_parser('signal')
    parser_string = subparsers.add_parser('string')
    parser_oval = subparsers.add_parser('oval')
    parser_liquid = subparsers.add_parser('liquid')
    parser_liquid = subparsers.add_parser('double_liquid')

    parser_disk_bars = subparsers.add_parser('disk_bars')
    parser_disk_bars.add_argument('--disk_path', required=True, help='Path to the image file for disk visualization')
    parser_disk_bars.add_argument('--disk_size', required=True)

    parser_circular_bars = subparsers.add_parser('circular_bars')
    parser_circular_bars.add_argument('--image_path', required=False, help='Path to the image file for circular_bars visualization')
    parser_circular_liquid = subparsers.add_parser('circular_liquid')
    parser_circular_liquid.add_argument('--image_path', required=False, help='Path to the image file for circular_liquid visualization')
    parser_circular_liquid = subparsers.add_parser('sharp_circular_liquid')
    parser_circular_liquid.add_argument('--image_path', required=False, help='Path to the image file for sharp_circular_liquid visualization')
    parser_circular_signal = subparsers.add_parser('circular_signal')
    parser_circular_signal.add_argument('--image_path', required=False, help='Path to the image file for circular_signal visualization')

    parser_tiles = subparsers.add_parser('tiles')
    parser_tiles.add_argument('--tile_height', required=True, help='Height of single tile')
    parser_revert_tiles = subparsers.add_parser('revert_tiles')
    parser_revert_tiles.add_argument('--tile_height', required=True, help='Height of single tile')
    parser_tiles_with_cap = subparsers.add_parser('tiles_with_cap')
    parser_tiles_with_cap.add_argument('--cap_color', required=True, help='Color of the cap')
    parser_tiles_with_cap.add_argument('--tile_height', required=True, help='Height of single tile')
    parser_revert_tiles_with_cap = subparsers.add_parser('revert_tiles_with_cap')
    parser_revert_tiles_with_cap.add_argument('--cap_color', required=True, help='Color of the cap')
    parser_revert_tiles_with_cap.add_argument('--tile_height', required=True, help='Height of single tile')

    parser_image = subparsers.add_parser('image')
    parser_image.add_argument('--image_path', required=True, help='Path to the image file for visualization')
    parser_image.add_argument('--fit', required=True)
    parser_image.add_argument('--mode', required=True)
    parser_image.add_argument('--background', required=False)
    parser_image.add_argument('--scale', type = float, required=False)

    parser_symmetric_bars = subparsers.add_parser('symmetric_bars')
    parser_symmetric_bars.add_argument('--image_path', required=False, help='Path to the image file for symmetric_bars visualization')
    parser_symmetric_bars.add_argument('--bar_height', required=True, help='Height of single tile')
    parser_symmetric_bars.add_argument('--background', required=False)
    parser_symmetric_bars.add_argument('--scale', type = float, required=False)
    parser_symmetric_bars.add_argument('--mode', required=False)
    
    args = parser.parse_args()
    main(args)