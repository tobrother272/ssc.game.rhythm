import cv2
import numpy as np
import argparse
import os
import uuid
import subprocess

fps = 24

def wave_distortion(image, amplitude, frequency, start_x):
    _, cols, _ = image.shape
    distorted_image = np.zeros_like(image)

    for i in range(cols):
        offset = int(amplitude * np.sin(2 * np.pi * (i - start_x) / frequency))
        distorted_image[:, i] = np.roll(image[:, i], offset, axis=0)
        if offset > 0:
            distorted_image[:offset, i] = image[:offset, i]
        elif offset < 0:
            distorted_image[offset:, i] = image[offset:, i]

    return distorted_image

def stack_frames(sample, seconds):
    step = len(sample)
    video_frames = []

    for _ in range(0, seconds * fps, step):
       video_frames += sample
    
    video_frames += video_frames[:(seconds * fps) % len(video_frames)]
    return video_frames

def apply_wave_distortion(video, amplitude=10, frequency=20, seconds=200):
    video_frames = []
    for i in range(0, frequency):
        distorted_image = wave_distortion(image, amplitude, frequency, i)
        video_frames.append(distorted_image)

    video_frames = stack_frames(video_frames, seconds)
    
    for frame in video_frames:
        video.write(frame)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Video filter tool')
    subparsers = parser.add_subparsers(dest='type', required=True, help='Type of filter')
    parser_wave_distortion = subparsers.add_parser('wave_distortion', help='Apply wave distortion to the video')
    parser_wave_distortion.add_argument('-i', '--input', required=True, help='Path to the image file')
    parser_wave_distortion.add_argument('-o', '--output', required=True, help='Path of the output video file')
    parser_wave_distortion.add_argument('-a', '--amplitude', required=False, type=int, default=10, help='Amplitude of the wave')
    parser_wave_distortion.add_argument('-f', '--frequency', required=False, type=int, default=20, help='Frequency of the wave')
    parser_wave_distortion.add_argument('-l', '--length', required=False, type=int, default=20, help='Length in seconds of the video')

    args = parser.parse_args()

    image = cv2.imread(args.input)
    height, width, _ = image.shape
    temp_file = "./temp/temp_video/" + str(uuid.uuid4()) + ".mp4"
    if not os.path.isdir("./temp/temp_video/"):
        os.mkdir("./temp/temp_video/")
    video = cv2.VideoWriter(temp_file, cv2.VideoWriter_fourcc(*'mp4v'), 24, (width, height))

    if args.type == 'wave_distortion':
        apply_wave_distortion(video, args.amplitude, args.frequency, args.length)

    video.release()

    subprocess.run(['ffmpeg', '-y', '-i', temp_file, '-c:v', 'libx265', '-c:a', 'aac', '-strict', 'experimental', args.output])
