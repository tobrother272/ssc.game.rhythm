import os
import sys
import numpy as np
import cv2
import argparse

def blend_frame(frame1, frame2, x, y):
    # Create a copy of frame2 to work on
    output_frame = np.copy(frame2)

    # Extract the RGB and alpha channels from both frames
    rgb1 = frame1[..., :3]
    alpha1 = frame1[..., 3:4] / 255.0

    # Calculate the size of frame1
    h1, w1 = frame1.shape[:2]
    
    # Extract the region of output_frame that will be affected by frame1
    rgb2 = output_frame[y:y+h1, x:x+w1, :3]
    alpha2 = output_frame[y:y+h1, x:x+w1, 3:4] / 255.0

    # Blend the RGB channels
    out_rgb = rgb1 * alpha1 + rgb2 * alpha2 * (1 - alpha1)

    # Blend the alpha channels
    out_alpha = alpha1 + alpha2 * (1 - alpha1)

    # Avoid division by zero in case both alphas are 0
    combined_alpha = np.maximum(out_alpha, 1e-10)
    
    # Normalize the blended RGB by the combined alpha to avoid darkening the output
    out_rgb /= combined_alpha

    # Scale alpha back to 255 range
    out_alpha *= 255

    # Place the blended result back into the corresponding region of output_frame
    output_frame[y:y+h1, x:x+w1, :3] = out_rgb
    output_frame[y:y+h1, x:x+w1, 3:4] = out_alpha

    return output_frame

def add_padding(frame, x, y, width, height):
    output_frame = np.zeros((height, width, frame.shape[2]), dtype=frame.dtype)

    # Calculate the dimensions of the area to copy from the original frame
    copy_width = min(frame.shape[1], width - x)
    copy_height = min(frame.shape[0], height - y)

    # Check if the coordinates are within the new frame bounds
    if x >= 0 and y >= 0 and x + copy_width <= width and y + copy_height <= height:
        # Place the appropriate region of the original frame into the new frame
        output_frame[y:y + copy_height, x:x + copy_width] = frame[:copy_height, :copy_width]

    return output_frame

def main(args):
    # Open the foreground video
    foreground = cv2.VideoCapture(args.foreground)
    if not foreground.isOpened():
        print(f"Error: Could not open video '{args.foreground}'.")
        sys.exit(1)

    # Open the background video
    background = cv2.VideoCapture(args.background)
    if not background.isOpened():
        print(f"Error: Could not open video '{args.background}'.")
        sys.exit(1)

    # Get the width and height of the foreground video
    width = int(background.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(background.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Create a VideoWriter object to save the output video
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    output = cv2.VideoWriter(args.output, fourcc, 30, (width, height))

    # Read the first frame of the foreground video
    ret1, frame1 = foreground.read()
    if not ret1:
        print("Error: Could not read the first frame of the foreground video.")
        sys.exit(1)

    # Read the first frame of the background video
    ret2, frame2 = background.read()
    if not ret2:
        print("Error: Could not read the first frame of the background video.")
        sys.exit(1)

    # Loop through the frames of the foreground video
    while ret1:
        # Blend the current frame of the foreground video with the current frame of the background video
        padded_frame1 = add_padding(frame1, args.x_position, args.y_position, width, height)
        frame = blend_frame(padded_frame1, frame2, args.x_position, args.y_position)

        # Write the frame to the output video
        output.write(frame)

        # Read the next frame of the foreground video
        ret1, frame1 = foreground.read()

        # Read the next frame of the background video
        ret2, frame2 = background.read()

    # Release the VideoCapture and VideoWriter objects
    foreground.release()
    background.release()
    output.release()

    print(f"Output video saved to '{args.output}'.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Combine two images with an alpha mask.")
    parser.add_argument("-f", "--foreground", required=True, help="Path to the foreground video.")
    parser.add_argument("-b", "--background", required=True, help="Path to the background video.")
    parser.add_argument("-o", "--output", required=True, help="Path to the output video.")
    parser.add_argument("-x", "--x-position", type=int, default=0, help="X position of the foreground video.")
    parser.add_argument("-y", "--y-position", type=int, default=0, help="Y position of the foreground video.")

    args = parser.parse_args()
    main(args)