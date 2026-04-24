import cv2
import numpy as np
import subprocess

def adjust_saturation(color, saturation_scale):
    # Convert the color to HSV
    color_hsv = cv2.cvtColor(np.uint8([[color]]), cv2.COLOR_RGB2HSV)
    # Adjust the saturation
    color_hsv[0, 0, 1] = np.clip(color_hsv[0, 0, 1] * saturation_scale, 0, 255)
    # Convert back to RGB
    color_rgb = cv2.cvtColor(color_hsv, cv2.COLOR_HSV2RGB)[0, 0]
    return (int(color_rgb[0]), int(color_rgb[1]), int(color_rgb[2]), color[3])

def create_single_frame(i, size):
    frames = []
    for j in range(6):
        frame = np.zeros((720, 1280, 4), dtype=np.uint8)
        
        # Draw a white line with varying alpha (transparency) values and thickness
        alpha_value = int(0.7 * j * 255 / 6)  # Correct calculation for alpha
        thickness = (size * (6 - j) // 3) + 2
        cv2.line(frame, (100, 100), (500, 500), adjust_saturation((255, 0, 0, alpha_value), 0.5), thickness=thickness, lineType=cv2.LINE_AA)
        
        frames.append(frame)

    frame = np.zeros((720, 1280, 4), dtype=np.uint8)
    cv2.line(frame, (100, 100), (500, 500), (255, 0, 0, 255), thickness=2, lineType=cv2.LINE_AA)
    frames.append(frame)
    return frames

def combine_frames(frames):
    combined_frame = np.zeros((720, 1280, 4), dtype=np.uint8)
    
    for frame in frames:
        # Alpha blend the current frame with the combined frame
        alpha_frame = frame[:, :, 3] / 255.0
        for c in range(0, 3):
            combined_frame[:, :, c] = combined_frame[:, :, c] * (1 - alpha_frame) + frame[:, :, c] * alpha_frame
        combined_frame[:, :, 3] = np.maximum(combined_frame[:, :, 3], frame[:, :, 3])
    
    return combined_frame

def save_combined_frames_as_images():
    for i in range(24):  # 24 frames
        frames = create_single_frame(i, i + 1)
        combined_frame = combine_frames(frames)
        
        # Save each combined frame as a PNG file with transparency
        cv2.imwrite(f'temp/temp_image/combined_frame_{i:03d}.png', combined_frame)

def create_video_with_ffmpeg():
    # FFmpeg command to create video with transparency
    command = [
        'ffmpeg', '-y', '-framerate', '24', '-i', 'temp/temp_image/combined_frame_%03d.png',
        '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuva420p', 'output/output_test.webm'
    ]
    
    # Run the FFmpeg command as a subprocess
    subprocess.run(command, check=True)

def main():
    save_combined_frames_as_images()
    create_video_with_ffmpeg()

if __name__ == "__main__":
    main()
