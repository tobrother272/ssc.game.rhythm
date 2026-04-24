import cv2
import numpy as np
from utils import *
from image_processing import *
import copy
import math

def draw_bars(frame, spectrum, n_bars, bar_color, option):
    mid_point = frame.shape[0] // 2
    col_width = frame.shape[1] // n_bars
    bar_width = col_width * 3 // 5

    x = np.array(range(n_bars)) * col_width + col_width // 2
    y1 = mid_point - (spectrum * mid_point).astype(int)
    y2 = mid_point + (spectrum * mid_point).astype(int)

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(n_bars):
                cv2.line(frame, (x[i], y1[i]), (x[i], y2[i]), glow_color[i], thickness=option['size'] + j, lineType=cv2.LINE_AA)

    for i in range(n_bars):
        cv2.line(frame, (x[i], y1[i]), (x[i], y2[i]), bar_color[i], thickness=bar_width, lineType=cv2.LINE_AA)

    if (option['glow'] != 0):
        for i in range(n_bars):
            cv2.line(frame, (x[i], y1[i]), (x[i], y2[i]), (255, 255, 255, 255), thickness=option['size'] // 10 + 1, lineType=cv2.LINE_AA)

def draw_up_bars(frame, spectrum, n_bars, bar_color, option):
    col_width = frame.shape[1] // n_bars
    bar_width = col_width * 3 // 5

    x = np.array(range(n_bars)) * frame.shape[1] // n_bars + col_width // 2
    y = frame.shape[0] - spectrum * frame.shape[0] - bar_width
    y = y.astype(int)

    if (option['glow'] != 0):
        glow_strength = option['glow']
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(n_bars):
                cv2.line(frame, (x[i], y[i]), (x[i], frame.shape[0] - bar_width), glow_color[i], thickness=option['size'] + j * 2, lineType=cv2.LINE_AA)
    
    for i in range(n_bars):
        # Draw round rectangle
        cv2.line(frame, (x[i], y[i]), (x[i], frame.shape[0] - bar_width), bar_color[i], thickness=bar_width, lineType=cv2.LINE_AA)

    if (option['glow'] != 0):
        for i in range(n_bars):
            cv2.line(frame, (x[i], y[i]), (x[i], frame.shape[0] - bar_width), (255, 255, 255, 255), thickness=option['size'] // 10 + 1, lineType=cv2.LINE_AA)

def draw_line(frame, spectrum, bar_color, option):
    x_values = np.linspace(0, frame.shape[1], len(spectrum) + 1)
    radius = (x_values[1] - x_values[0]) / 2
    x_values += radius

    mid_point = frame.shape[0] // 2
    up_y = mid_point - spectrum * mid_point
    down_y = mid_point + spectrum * mid_point

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(0, len(up_y) - 1, 2):
                y_size = int(down_y[i] - up_y[i] if down_y[i] - up_y[i] < radius else radius)
                cv2.line(frame, (int(x_values[i]), int(up_y[i])), (int(x_values[i]), int(down_y[i])), glow_color[i], thickness=option['size'] + j * 2, lineType=cv2.LINE_AA)
                cv2.ellipse(frame, (int(x_values[i] + radius), int(up_y[i])), (int(radius), y_size), 180, 0, 180, glow_color[i], thickness=option['size'] + j * 2, lineType=cv2.LINE_AA)

            for i in range(1, len(up_y) - 2, 2):
                y_size = int(down_y[i] - up_y[i] if down_y[i] - up_y[i] < radius else radius)
                cv2.line(frame, (int(x_values[i]), int(up_y[i - 1])), (int(x_values[i]), int(down_y[i + 1])), glow_color[i], thickness=option['size'] + j * 2, lineType=cv2.LINE_AA)
                cv2.ellipse(frame, (int(x_values[i] + radius), int(down_y[i + 1])), (int(radius), y_size), 0, 0, 180, glow_color[i], thickness=option['size'] + j * 2, lineType=cv2.LINE_AA)

    for i in range(0, len(up_y) - 1, 2):
        y_size = int(down_y[i] - up_y[i] if down_y[i] - up_y[i] < radius else radius)
        cv2.line(frame, (int(x_values[i]), int(up_y[i])), (int(x_values[i]), int(down_y[i])), bar_color[i], thickness=option['size'], lineType=cv2.LINE_AA)
        cv2.ellipse(frame, (int(x_values[i] + radius), int(up_y[i])), (int(radius), y_size), 180, 0, 180, bar_color[i], thickness=option['size'], lineType=cv2.LINE_AA)

    for i in range(1, len(up_y) - 2, 2):
        y_size = int(down_y[i] - up_y[i] if down_y[i] - up_y[i] < radius else radius)
        cv2.line(frame, (int(x_values[i]), int(up_y[i - 1])), (int(x_values[i]), int(down_y[i + 1])), bar_color[i], thickness=option['size'], lineType=cv2.LINE_AA)
        cv2.ellipse(frame, (int(x_values[i] + radius), int(down_y[i + 1])), (int(radius), y_size), 0, 0, 180, bar_color[i], thickness=option['size'], lineType=cv2.LINE_AA)

    if (option['glow'] != 0):
        for i in range(0, len(up_y) - 1, 2):
            y_size = int(down_y[i] - up_y[i] if down_y[i] - up_y[i] < radius else radius)
            cv2.line(frame, (int(x_values[i]), int(up_y[i])), (int(x_values[i]), int(down_y[i])),  (255, 255, 255, 255), thickness=option['size'] // 10 + 1, lineType=cv2.LINE_AA)
            cv2.ellipse(frame, (int(x_values[i] + radius), int(up_y[i])), (int(radius), y_size), 180, 0, 180, (255, 255, 255, 255), thickness=option['size'] // 10 + 1, lineType=cv2.LINE_AA)

        for i in range(1, len(up_y) - 2, 2):
            y_size = int(down_y[i] - up_y[i] if down_y[i] - up_y[i] < radius else radius)
            cv2.line(frame, (int(x_values[i]), int(up_y[i - 1])), (int(x_values[i]), int(down_y[i + 1])), (255, 255, 255, 255), thickness=option['size'] // 10 + 1, lineType=cv2.LINE_AA)
            cv2.ellipse(frame, (int(x_values[i] + radius), int(down_y[i + 1])), (int(radius), y_size), 0, 0, 180, (255, 255, 255, 255), thickness=option['size'] // 10 + 1, lineType=cv2.LINE_AA)

def draw_tiles(frame, spectrum, n_bars, color_mode, option):
    tile_space = 4
    top_hue = 0  # Red color in HSV
    bottom_hue = 240  # Hue for the bottom-most color (e.g., 120 for green)
    col_width = frame.shape[1] // n_bars
    bar_width = col_width - 4
    tile_height = (frame.shape[0] - (tile_space * (option['max_tiles'] - 1))) // option['max_tiles']

    x1 = np.array(range(n_bars)) * col_width
    x2 = x1 + bar_width

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for k in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - k) / (glow_strength * 2)
            for i in range(n_bars):
                num_tiles = (int(spectrum[i] * option['max_tiles']))
                y2 = frame.shape[0] - np.array(range(num_tiles)) * (tile_height + tile_space)
                y1 = y2 - tile_height
                y = (y1 + y2) // 2

                for j in range(num_tiles):
                    # Compute the hue for the tile
                    if color_mode == 'gradient':
                        hue = top_hue + ((bottom_hue - top_hue) * j / option['max_tiles'])
                        hsv_color = np.array([[[hue, 255, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
                    else:
                        h, s, v = hex_to_hsv(color_mode)
                        hsv_color = np.array([[[h, s, v]]], dtype=np.uint8)  # Reshaped to 1x1x3
                    
                    bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
                    if option['mp4']:
                        tile_color = list(int(c * alpha) for c in bgr_color)  # Convert to integer
                        tile_color.append(0)    
                    else:
                        tile_color = list(int(c) for c in bgr_color)  # Convert to integer
                        tile_color.append(int(255 * alpha))

                    cv2.line(frame, (x1[i], y[j]), (x2[i], y[j]), tile_color, thickness=k * 2)

    for i in range(n_bars):
        num_tiles = (int(spectrum[i] * option['max_tiles']))
        y2 = frame.shape[0] - np.array(range(num_tiles)) * (tile_height + tile_space)
        y1 = y2 - tile_height

        for j in range(num_tiles):
            # Compute the hue for the tile
            if color_mode == 'gradient':
                hue = top_hue + ((bottom_hue - top_hue) * j / option['max_tiles'])
                hsv_color = np.array([[[hue, 255, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
            else:
                h, s, v = hex_to_hsv(color_mode)
                hsv_color = np.array([[[h, s, v]]], dtype=np.uint8)  # Reshaped to 1x1x3
            
            bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
            tile_color = list(int(c) for c in bgr_color)  # Convert to integer
            tile_color.append(255)

            cv2.rectangle(frame, (x1[i], y1[j]), (x2[i], y2[j]), tile_color, thickness=cv2.FILLED)

def draw_tiles_with_cap(frame, spectrum, n_bars, color_mode, option):
    tile_space = 4
    top_hue = 0  # Red color in HSV
    bottom_hue = 240  # Hue for the bottom-most color (e.g., 120 for green)
    col_width = frame.shape[1] // n_bars
    bar_width = col_width - 4
    tile_height = (frame.shape[0] - (tile_space * (option['max_tiles'] - 1))) // option['max_tiles']

    x1 = np.array(range(n_bars)) * col_width
    x2 = x1 + bar_width

    option['prev_max'] += np.full(n_bars, tile_height + tile_space)
    current_max = np.zeros_like(option['prev_max'])

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for k in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - k) / (glow_strength * 2)
            for i in range(n_bars):
                num_tiles = (int(spectrum[i] * option['max_tiles']))
                current_max[i] = frame.shape[0] - (int(spectrum[i] * option['max_tiles'])) * (tile_height + tile_space)

                y2 = frame.shape[0] - np.array(range(num_tiles)) * (tile_height + tile_space)
                y1 = y2 - tile_height
                y = (y1 + y2) // 2

                for j in range(num_tiles):
                    # Compute the hue for the tile
                    if color_mode == 'gradient':
                        hue = top_hue + ((bottom_hue - top_hue) * j / option['max_tiles'])
                        hsv_color = np.array([[[hue, 255, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
                    else:
                        h, s, v = hex_to_hsv(color_mode)
                        hsv_color = np.array([[[h, s, v]]], dtype=np.uint8)  # Reshaped to 1x1x3
                    
                    bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
                    if option['mp4']:
                        tile_color = list(int(c * alpha) for c in bgr_color)  # Convert to integer
                        tile_color.append(0)    
                    else:
                        tile_color = list(int(c) for c in bgr_color)  # Convert to integer
                        tile_color.append(int(255 * alpha))

                    cv2.line(frame, (x1[i], y[j]), (x2[i], y[j]), tile_color, thickness=k * 2)

            current_max = np.min([current_max, option['prev_max']], axis = 0)
            y2 = current_max
            y1 = y2 - tile_height
            y = (y1 + y2) // 2

            for i in range(n_bars):
                if option['cap_color'] == 'gradient':
                    hue = top_hue + (int)(spectrum[i] * (bottom_hue - top_hue))
                    hsv_color = np.array([[[hue, 200, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
                else:
                    h, s, v = hex_to_hsv(option['cap_color'])
                    hsv_color = np.array([[[h, s, v]]], dtype=np.uint8)  # Reshaped to 1x1x3

                bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
                tile_color = list(int(c) for c in bgr_color)  # Convert to integer
                tile_color.append(int(255 * alpha))

                cv2.line(frame, (x1[i], y[i]), (x2[i], y[i]), tile_color, thickness=k * 2)

    option['prev_max'] += np.full(n_bars, tile_height + tile_space)
    current_max = np.zeros_like(option['prev_max'])

    for i in range(n_bars):
        num_tiles = (int(spectrum[i] * option['max_tiles']))
        current_max[i] = frame.shape[0] - (int(spectrum[i] * option['max_tiles'])) * (tile_height + tile_space)

        y2 = frame.shape[0] - np.array(range(num_tiles)) * (tile_height + tile_space)
        y1 = y2 - tile_height

        for j in range(num_tiles):
            # Compute the hue for the tile
            if color_mode == 'gradient':
                hue = top_hue + ((bottom_hue - top_hue) * j / option['max_tiles'])
                hsv_color = np.array([[[hue, 255, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
            else:
                h, s, v = hex_to_hsv(color_mode)
                hsv_color = np.array([[[h, s, v]]], dtype=np.uint8)  # Reshaped to 1x1x3
            
            bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
            tile_color = list(int(c) for c in bgr_color)  # Convert to integer
            tile_color.append(255)

            cv2.rectangle(frame, (x1[i], y1[j]), (x2[i], y2[j]), tile_color, thickness=cv2.FILLED)

    current_max = np.min([current_max, option['prev_max']], axis = 0)
    y2 = current_max
    y1 = y2 - tile_height

    for i in range(n_bars):
        if option['cap_color'] == 'gradient':
            hue = top_hue + (int)(spectrum[i] * (bottom_hue - top_hue))
            hsv_color = np.array([[[hue, 200, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
        else:
            h, s, v = hex_to_hsv(option['cap_color'])
            hsv_color = np.array([[[h, s, v]]], dtype=np.uint8)  # Reshaped to 1x1x3

        bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
        tile_color = list(int(c) for c in bgr_color)  # Convert to integer

        cv2.rectangle(frame, (x1[i], y1[i]), (x2[i], y2[i]), tile_color, thickness=cv2.FILLED)
    option['prev_max'] = current_max

def draw_revert_tiles(frame, spectrum, n_bars, color_mode, option):
    tile_space = 4
    top_hue = 0  # Red color in HSV
    bottom_hue = 200  # Hue for the bottom-most color (e.g., 120 for green)
    col_width = frame.shape[1] // n_bars
    bar_width = col_width - 4
    tile_height = (frame.shape[0] - (tile_space * (option['max_tiles'] - 1))) // option['max_tiles']

    x1 = np.array(range(n_bars)) * col_width
    x2 = x1 + bar_width

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for k in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - k) / (glow_strength * 2)
            for i in range(n_bars):
                num_tiles = (int(spectrum[i] * option['max_tiles']))
                y2 = frame.shape[0] - np.array(range(num_tiles)) * (tile_height + tile_space)
                y1 = y2 - tile_height
                y = (y1 + y2) // 2

                for j in range(num_tiles):
                    # Compute the hue for the tile
                    if color_mode == 'gradient':
                        hue = top_hue + ((bottom_hue - top_hue) * (num_tiles - 1 - j) / option['max_tiles'])
                        hsv_color = np.array([[[hue, 255, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
                    else:
                        h, s, v = hex_to_hsv(color_mode)
                        hsv_color = np.array([[[h, s, v]]], dtype=np.uint8)  # Reshaped to 1x1x3
                    
                    bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
                    if option['mp4']:
                        tile_color = list(int(c * alpha) for c in bgr_color)  # Convert to integer
                        tile_color.append(0)    
                    else:
                        tile_color = list(int(c) for c in bgr_color)  # Convert to integer
                        tile_color.append(int(255 * alpha))

                    cv2.line(frame, (x1[i], y1[j]), (x2[i], y1[j]), tile_color, thickness=k * 2)

    for i in range(n_bars):
        num_tiles = (int(spectrum[i] * option['max_tiles']))
        y2 = frame.shape[0] - np.array(range(num_tiles)) * (tile_height + tile_space)
        y1 = y2 - tile_height

        for j in range(num_tiles):
            # Compute the hue for the tile
            if color_mode == 'gradient':
                hue = top_hue + ((bottom_hue - top_hue) * (num_tiles - 1 - j) / option['max_tiles'])
                hsv_color = np.array([[[hue, 255, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
            else:
                hue = hex_to_hue(color_mode)
                hsv_color = np.array([[[hue, 255, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
            
            bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
            tile_color = list(int(c) for c in bgr_color)  # Convert to integer
            tile_color.append(255)

            cv2.rectangle(frame, (x1[i], y1[j]), (x2[i], y2[j]), tile_color, thickness=cv2.FILLED)

def draw_revert_tiles_with_cap(frame, spectrum, n_bars, color_mode, option):
    tile_space = 4
    top_hue = 0  # Red color in HSV
    bottom_hue = 200  # Hue for the bottom-most color (e.g., 120 for green)
    col_width = frame.shape[1] // n_bars
    bar_width = col_width - 4
    tile_height = (frame.shape[0] - (tile_space * (option['max_tiles'] - 1))) // option['max_tiles']

    x1 = np.array(range(n_bars)) * col_width
    x2 = x1 + bar_width

    option['prev_max'] += np.full(n_bars, tile_height + tile_space)
    current_max = np.zeros_like(option['prev_max'])

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for k in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - k) / (glow_strength * 2)
            for i in range(n_bars):
                num_tiles = (int(spectrum[i] * option['max_tiles']))
                current_max[i] = frame.shape[0] - (int(spectrum[i] * option['max_tiles'])) * (tile_height + tile_space)

                y2 = frame.shape[0] - np.array(range(num_tiles)) * (tile_height + tile_space)
                y1 = y2 - tile_height
                y = (y1 + y2) // 2

                for j in range(num_tiles):
                    # Compute the hue for the tile
                    if color_mode == 'gradient':
                        hue = top_hue + ((bottom_hue - top_hue) * (num_tiles - 1 - j) / option['max_tiles'])
                        hsv_color = np.array([[[hue, 255, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
                    else:
                        h, s, v = hex_to_hsv(color_mode)
                        hsv_color = np.array([[[h, s, v]]], dtype=np.uint8)  # Reshaped to 1x1x3
                    
                    bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
                    if option['mp4']:
                        tile_color = list(int(c * alpha) for c in bgr_color)  # Convert to integer
                        tile_color.append(0)    
                    else:
                        tile_color = list(int(c) for c in bgr_color)  # Convert to integer
                        tile_color.append(int(255 * alpha))

                    cv2.line(frame, (x1[i], y[j]), (x2[i], y[j]), tile_color, thickness=k * 2)

            current_max = np.min([current_max, option['prev_max']], axis = 0)
            y2 = current_max
            y1 = y2 - tile_height
            y = (y1 + y2) // 2

            for i in range(n_bars):
                if option['cap_color'] == 'gradient':
                    hsv_color = np.array([[[top_hue, 200, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
                else:
                    h, s, v = hex_to_hsv(option['cap_color'])
                    hsv_color = np.array([[[h, s, v]]], dtype=np.uint8)  # Reshaped to 1x1x3

                bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
                tile_color = list(int(c) for c in bgr_color)  # Convert to integer
                tile_color.append(int(255 * alpha))

                cv2.line(frame, (x1[i], y[i]), (x2[i], y[i]), tile_color, thickness=k * 2)

    option['prev_max'] += np.full(n_bars, tile_height + tile_space)
    current_max = np.zeros_like(option['prev_max'])

    for i in range(n_bars):
        num_tiles = (int(spectrum[i] * option['max_tiles']))
        current_max[i] = frame.shape[0] - (int(spectrum[i] * option['max_tiles'])) * (tile_height + tile_space)
        y2 = frame.shape[0] - np.array(range(num_tiles)) * (tile_height + tile_space)
        y1 = y2 - tile_height

        for j in range(num_tiles):
            # Compute the hue for the tile
            if color_mode == 'gradient':
                hue = top_hue + ((bottom_hue - top_hue) * (num_tiles - 1 - j) / option['max_tiles'])
                hsv_color = np.array([[[hue, 255, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
            else:
                hue = hex_to_hue(color_mode)
                hsv_color = np.array([[[hue, 255, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
            
            bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
            tile_color = list(int(c) for c in bgr_color)  # Convert to integer
            tile_color.append(255)

            cv2.rectangle(frame, (x1[i], y1[j]), (x2[i], y2[j]), tile_color, thickness=cv2.FILLED)

    current_max = np.min([current_max, option['prev_max']], axis = 0)
    y2 = current_max
    y1 = y2 - tile_height
    for i in range(n_bars):
        if option['cap_color'] == 'gradient':
            hsv_color = np.array([[[top_hue, 200, 255]]], dtype=np.uint8)  # Reshaped to 1x1x3
        else:
            h, s, v = hex_to_hsv(option['cap_color'])
            hsv_color = np.array([[[h, s, v]]], dtype=np.uint8)  # Reshaped to 1x1x3
        
        bgr_color = cv2.cvtColor(hsv_color, cv2.COLOR_HSV2BGR)[0][0]
        tile_color = list(int(c) for c in bgr_color)  # Convert to integer
        tile_color.append(255)

        cv2.rectangle(frame, (x1[i], y1[i]), (x2[i], y2[i]), tile_color, thickness=cv2.FILLED)
    option['prev_max'] = current_max

def draw_circular_bars(frame, spectrum, n_bars, bar_color, option):
    rotation_speed = 0.5
    inner_circle_radius = frame.shape[0] // 6
    center_x, center_y = frame.shape[1] // 2, frame.shape[0] // 2
    max_bar_length = min(center_x, center_y) - inner_circle_radius
    rotation_angle = (option['frame_number'] * rotation_speed) % 360

    spectrum = spectrum[len(spectrum) // 6: len(spectrum) // 6 * 5]
    n_bars = len(spectrum)

    bar_length = spectrum * max_bar_length
    angle = ((180 / n_bars) * np.array(range(n_bars)) + rotation_angle)
    mirrored_angle = angle + 180

    start_x = (center_x + inner_circle_radius * np.cos(np.radians(angle))).astype(int)
    start_y = (center_y + inner_circle_radius * np.sin(np.radians(angle))).astype(int)
    end_x = (center_x + (inner_circle_radius + bar_length) * np.cos(np.radians(angle))).astype(int)
    end_y = (center_y + (inner_circle_radius + bar_length) * np.sin(np.radians(angle))).astype(int)

    mirrored_start_x = (center_x + inner_circle_radius * np.cos(np.radians(mirrored_angle))).astype(int)
    mirrored_start_y = (center_y + inner_circle_radius * np.sin(np.radians(mirrored_angle))).astype(int)
    mirrored_end_x = (center_x + (inner_circle_radius + bar_length) * np.cos(np.radians(mirrored_angle))).astype(int)
    mirrored_end_y = (center_y + (inner_circle_radius + bar_length) * np.sin(np.radians(mirrored_angle))).astype(int)

    if (option['glow'] != 0):
        glow_strength = option['glow']
        
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(n_bars):
                cv2.line(frame, (start_x[i], start_y[i]), (end_x[i], end_y[i]), glow_color[i], thickness=option['size'] + j * 2, lineType=cv2.LINE_AA)
                cv2.line(frame, (mirrored_start_x[i], mirrored_start_y[i]), (mirrored_end_x[i], mirrored_end_y[i]), glow_color[-i], thickness=option['size'] + j * 2, lineType=cv2.LINE_AA)

    for i in range(n_bars):
        cv2.line(frame, (start_x[i], start_y[i]), (end_x[i], end_y[i]), bar_color[i], thickness=option['size'], lineType=cv2.LINE_AA)
        cv2.line(frame, (mirrored_start_x[i], mirrored_start_y[i]), (mirrored_end_x[i], mirrored_end_y[i]), bar_color[-i], thickness=option['size'], lineType=cv2.LINE_AA)

    if (option['glow'] != 0):
        for i in range(n_bars):
            cv2.line(frame, (start_x[i], start_y[i]), (end_x[i], end_y[i]), (255, 255, 255, 255), thickness=option['size'] // 10 + 1, lineType=cv2.LINE_AA)
            cv2.line(frame, (mirrored_start_x[i], mirrored_start_y[i]), (mirrored_end_x[i], mirrored_end_y[i]), (255, 255, 255, 255), thickness=option['size'] // 10 + 1, lineType=cv2.LINE_AA)

def draw_signal(frame, spectrum, bar_color, option):
    x = np.linspace(0, frame.shape[1], len(spectrum))
    y = np.zeros_like(x)

    mid_point = frame.shape[0] // 2
    up_y = mid_point - spectrum * mid_point
    down_y = mid_point + spectrum * mid_point
    y[::2] = down_y[::2]
    y[1::2] = up_y[1::2]

    if (option['glow'] != 0):
        glow_strength = option['glow']
        
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(len(y) - 1):
                cv2.line(frame, (int(x[i]), int(y[i])), (int(x[i + 1]), int(y[i + 1])), glow_color[i], thickness=option['size'] + j * 2, lineType=cv2.LINE_AA)

    for i in range(len(y) - 1):
        cv2.line(frame, (int(x[i]), int(y[i])), (int(x[i + 1]), int(y[i + 1])), bar_color[i], thickness=option['size'], lineType=cv2.LINE_AA)

def draw_string(frame, spectrum, color_mode, option):
    mid_point = frame.shape[0] // 2
    n_bars = len(spectrum)
    norm_spectrum = spectrum * mid_point

    x = np.arange(0, frame.shape[1], frame.shape[1] // 300)
    peak_index = option['peak']
    maxy_index = peak_index * frame.shape[1] // 30

    if (peak_index == 9 or peak_index == 21):
        height_ratio = 1
    else:
        height_ratio = 0.5

    bar_height = (hillarize(x, maxy_index) * norm_spectrum[peak_index * n_bars // 30] * height_ratio).astype(int)
    y1 = mid_point - bar_height
    y2 = mid_point + bar_height

    bar_color = [get_bar_color(i, len(x), color_mode) for i in range(len(x) - 1)]

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(len(x) - 1):
                cv2.line(frame, (x[i], y1[i]), (x[i + 1], y1[i + 1]), glow_color[i], thickness=option['size'] * j, lineType=cv2.LINE_AA)
                cv2.line(frame, (x[i], y2[i]), (x[i + 1], y2[i + 1]), glow_color[i], thickness=option['size'] * j, lineType=cv2.LINE_AA)

    # Draw the main lines on top
    for i in range(len(x) - 1):
        cv2.line(frame, (x[i], y1[i]), (x[i + 1], y1[i + 1]), bar_color[i], thickness=option['size'] + 1, lineType=cv2.LINE_AA)
        cv2.line(frame, (x[i], y2[i]), (x[i + 1], y2[i + 1]), bar_color[i], thickness=option['size'] + 1, lineType=cv2.LINE_AA)

    if (option['glow'] != 0):
        for i in range(len(x) - 1):
            cv2.line(frame, (x[i], y1[i]), (x[i + 1], y1[i + 1]), (255, 255, 255, 255), thickness=option['size'], lineType=cv2.LINE_AA)
            cv2.line(frame, (x[i], y2[i]), (x[i + 1], y2[i + 1]), (255, 255, 255, 255), thickness=option['size'], lineType=cv2.LINE_AA)
        

def draw_oval(frame, spectrum, n_bars, bar_color, option):
    mid_point = frame.shape[0] // 2
    bar_width = frame.shape[1] // n_bars
    bar_width -= 15
    if bar_width % 2 == 1:
        bar_width += 1
    x = np.array(range(n_bars)) * frame.shape[1] // n_bars
    bar_height = (spectrum * frame.shape[0] // 2).astype(int)

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(n_bars):
                # Draw round rectangle
                cv2.ellipse(frame, (x[i], mid_point), (bar_width // 2 + j * 2, bar_height[i] + j * 2), 0, 0, 360, glow_color[i], thickness=cv2.FILLED, lineType=cv2.LINE_AA)

    for i in range(n_bars):
        # Draw round rectangle
        cv2.ellipse(frame, (x[i], mid_point), (bar_width // 2, bar_height[i]), 0, 0, 360, bar_color[i], thickness=cv2.FILLED, lineType=cv2.LINE_AA)

def draw_liquid(frame, spectrum, color_mode, option): 
    mid_point = frame.shape[0] // 2
    n_bars = len(spectrum)

    y = mid_point - spectrum * frame.shape[0] // 2

    y = np.concatenate([y[0:n_bars * 2 // 5:2], y[n_bars *  2 // 5:n_bars *3 // 5], y[n_bars * 3 // 5::2]])
    x = np.linspace(0, frame.shape[1], len(y))
 
    coefficients = np.polyfit(x, y, 17)
    polynomial = np.poly1d(coefficients)

    smooth_factor = 600
    size = frame.shape[1] // smooth_factor + 2

    x = np.linspace(0, frame.shape[1] - 1, smooth_factor).astype(int)
    y1 = polynomial(x).astype(int)
    y2 = frame.shape[0] - polynomial(x).astype(int)
    bar_color = [get_bar_color(i, len(x), color_mode) for i in range(len(x) - 1)]

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(len(x) - 1):
                cv2.line(frame, (x[i], y1[i]), (x[i], y1[i + 1]), glow_color[i], thickness=size + j * 2, lineType=cv2.LINE_AA)
                cv2.line(frame, (x[i], y2[i]), (x[i], y2[i + 1]), glow_color[i], thickness=size + j * 2, lineType=cv2.LINE_AA)

    for i in range(len(x) - 1):
        cv2.line(frame, (x[i], y1[i]), (x[i], y2[i]), bar_color[i], thickness=size, lineType=cv2.LINE_AA)

def draw_circular_liquid(frame, spectrum, color_mode, option):
    frame_number = option['frame_number']
    size = option['size']
    rotation_speed = 0.5
    
    inner_circle_radius = frame.shape[0] // 6
    center_x, center_y = frame.shape[1] // 2, frame.shape[0] // 2
    max_bar_length = min(center_x, center_y) - inner_circle_radius
    rotation_angle = (frame_number * rotation_speed) % 360

    y = spectrum * frame.shape[0] // 2
    n_bars = len(y)
    y = np.concatenate([y[n_bars *  5 // 12:n_bars *7 // 12] / 2, y[0:n_bars * 1 // 6], y[n_bars *  5 // 12:n_bars *7 // 12] / 2, y[n_bars * 5 // 6::], y[n_bars *  5 // 12:n_bars *7 // 12] / 2])
    x = np.linspace(0, frame.shape[1], len(y))

    coefficients = np.polyfit(x, y, 17)
    polynomial = np.poly1d(coefficients)

    smooth_factor = 600
    x = np.linspace(0, frame.shape[1] - 1, smooth_factor).astype(int)
    y = abs(polynomial(x).astype(int))

    bar_length = np.interp(y, (0, min(center_x, center_y)), (0, max_bar_length))
    bar_length = bar_length[50:550]
    bar_color = [get_bar_color(i, len(x), color_mode) for i in range(len(bar_length))]

    iter = np.array(range(len(bar_length)))
    angle = (180 / len(bar_length)) * iter + rotation_angle
    mirrored_angle = rotation_angle - (180 / len(bar_length)) * iter

    start_x = (center_x + inner_circle_radius * np.cos(np.radians(angle))).astype(int)
    start_y = (center_y + inner_circle_radius * np.sin(np.radians(angle))).astype(int)
    end_x = (center_x + (inner_circle_radius + bar_length) * np.cos(np.radians(angle))).astype(int)
    end_y = (center_y + (inner_circle_radius + bar_length) * np.sin(np.radians(angle))).astype(int)

    mirrored_start_x = (center_x + inner_circle_radius * np.cos(np.radians(mirrored_angle))).astype(int)
    mirrored_start_y = (center_y + inner_circle_radius * np.sin(np.radians(mirrored_angle))).astype(int)
    mirrored_end_x = (center_x + (inner_circle_radius + bar_length) * np.cos(np.radians(mirrored_angle))).astype(int)
    mirrored_end_y = (center_y + (inner_circle_radius + bar_length) * np.sin(np.radians(mirrored_angle))).astype(int)

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(len(bar_length) - 1):
                cv2.line(frame, (end_x[i], end_y[i]), (end_x[i + 1], end_y[i + 1]), glow_color[i], thickness=size + j * 2, lineType=cv2.LINE_AA)
                cv2.line(frame, (mirrored_end_x[i], mirrored_end_y[i]), (mirrored_end_x[i + 1], mirrored_end_y[i + 1]), glow_color[i], thickness=size + j * 2, lineType=cv2.LINE_AA)

    for i in range(len(bar_length)):
        cv2.line(frame, (start_x[i], start_y[i]), (end_x[i], end_y[i]), bar_color[i], thickness=size, lineType=cv2.LINE_AA)
        cv2.line(frame, (mirrored_start_x[i], mirrored_start_y[i]), (mirrored_end_x[i], mirrored_end_y[i]), bar_color[i], thickness=size, lineType=cv2.LINE_AA)

def draw_circular_signal(frame, spectrum, color_mode, option):
    frame_number = option['frame_number']
    size = option['size']
    rotation_speed = 0.5

    mean_y = np.mean(spectrum)
    y = spectrum * frame.shape[0]

    mid_radius = frame.shape[0] // 4 + frame.shape[0] // 5 * mean_y
    center_x, center_y = frame.shape[1] // 2, frame.shape[0] // 2
    rotation_angle = (frame_number * rotation_speed) % 360  

    y = np.interp(y, (0, frame.shape[0]), (0, mid_radius // 6))
    n_bars = len(y)
    y = np.concatenate([y[5 * n_bars // 12: 7 * n_bars // 12], y[2 * n_bars // 12: 4 * n_bars // 12], y[7 * n_bars // 12: 5 * n_bars // 12: -1]])
    y = np.concatenate([y, y, y])
    n_bars = len(y)
    bar_color = [get_bar_color(i, n_bars, color_mode) for i in range(n_bars)]

    if (option['glow'] != 0):
        glow_strength = int(option['glow'])
        
        for k in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - k) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(0, n_bars - 1, 2):
                start_angle = (360 / n_bars * i + rotation_angle)
                mid_angle = (360 / n_bars * (i + 1) + rotation_angle)
                end_angle = (360 / n_bars * (i + 2) + rotation_angle)
                
                start_x = int((center_x + (mid_radius - y[i % n_bars]) * np.cos(np.radians(start_angle))))
                start_y = int((center_y + (mid_radius - y[i % n_bars]) * np.sin(np.radians(start_angle))))

                mid_x = int((center_x + (mid_radius + y[(i + 1) % n_bars]) * np.cos(np.radians(mid_angle))))
                mid_y = int((center_y + (mid_radius + y[(i + 1) % n_bars]) * np.sin(np.radians(mid_angle))))

                end_x = int((center_x + (mid_radius - y[(i + 2) % n_bars]) * np.cos(np.radians(end_angle))))
                end_y = int((center_y + (mid_radius - y[(i + 2) % n_bars]) * np.sin(np.radians(end_angle))))

                cv2.line(frame, (start_x, start_y), (mid_x, mid_y), glow_color[i], thickness=max(size * k // 2, 1), lineType=cv2.LINE_AA)
                cv2.line(frame, (mid_x, mid_y), (end_x, end_y), glow_color[i + 1], thickness=max(size * k // 2, 1), lineType=cv2.LINE_AA)

    for i in range(0, n_bars, 2):
        start_angle = (360 / n_bars * i + rotation_angle)
        mid_angle = (360 / n_bars * (i + 1) + rotation_angle)
        end_angle = (360 / n_bars * (i + 2) + rotation_angle)
        
        start_x = int((center_x + (mid_radius - y[i % n_bars]) * np.cos(np.radians(start_angle))))
        start_y = int((center_y + (mid_radius - y[i % n_bars]) * np.sin(np.radians(start_angle))))

        mid_x = int((center_x + (mid_radius + y[(i + 1) % n_bars]) * np.cos(np.radians(mid_angle))))
        mid_y = int((center_y + (mid_radius + y[(i + 1) % n_bars]) * np.sin(np.radians(mid_angle))))

        end_x = int((center_x + (mid_radius - y[(i + 2) % n_bars]) * np.cos(np.radians(end_angle))))
        end_y = int((center_y + (mid_radius - y[(i + 2) % n_bars]) * np.sin(np.radians(end_angle))))
        
        cv2.line(frame, (start_x, start_y), (mid_x, mid_y), bar_color[i % n_bars], thickness=max(size, 1), lineType=cv2.LINE_AA)
        cv2.line(frame, (mid_x, mid_y), (end_x, end_y), bar_color[(i + 1) % n_bars], thickness=max(size, 1), lineType=cv2.LINE_AA)

def draw_image(frame, spectrum, option):
    image = option['image']
    
    if option['mode'] == 'treble':
        mean_volumn = np.mean(spectrum)
        mean_volumn = max(0, (mean_volumn - 0.6) / 0.4)
    elif option['mode'] == 'bass':
        mean_volumn = np.mean(spectrum[15:18])

    target_scale = 1 + mean_volumn * option['scale']
    scaled_image = zoom_image(image, target_scale)

    center_x = frame.shape[1] // 2
    center_y = frame.shape[0] // 2
    if 'x' in option and 'y' in option:
        center_x = option['x'] + image.shape[1] // 2
        center_y = option['y'] + image.shape[0] // 2

    image_center_x = scaled_image.shape[1] // 2
    image_center_y = scaled_image.shape[0] // 2

    overlay_image_alpha(frame, scaled_image, center_x - image_center_x, center_y - image_center_y)

def draw_double_liquid(frame, spectrum, color_mode, option):
    n_bars = len(spectrum)

    front_frame = np.zeros_like(frame)
    back_frame = np.zeros_like(frame)
    glow_frame = np.zeros_like(frame)
    white_glow_frame = np.zeros_like(frame)

    y = spectrum * frame.shape[0]

    y = np.concatenate([y[0:n_bars * 2 // 5:2], y[n_bars *  2 // 5:n_bars *3 // 5]])
    x = np.linspace(0, frame.shape[1], len(y))
 
    coefficients = np.polyfit(x, y, 12)
    polynomial = np.poly1d(coefficients)

    smooth_factor = 600
    size = frame.shape[1] // smooth_factor + 2

    x = np.linspace(0, frame.shape[1] - 1, smooth_factor).astype(int)
    y = frame.shape[0] - abs(polynomial(x).astype(int))
    bar_color = [get_bar_color(i, len(x), color_mode) for i in range(len(x) - 1)]

    right_y = copy.deepcopy(y)
    mask_y = np.logspace(-0.5, 0, len(right_y) // 2, base=10, endpoint=True)
    left_y = right_y[::-1]

    white = (255, 255, 255, 255)
    if option['glow'] != 0:
        glow_strength = option['glow']
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [(c * pixel_m) for c in bar_color]
            for i in range(len(x) - 1):
                cv2.line(glow_frame, (x[i], frame.shape[0]), (x[i], left_y[i]), glow_color[i], thickness=size + j, lineType=cv2.LINE_AA)

    for i in range(len(x) - 1):
        cv2.line(back_frame, (x[i], frame.shape[0]), (x[i], right_y[i]), white, thickness=size, lineType=cv2.LINE_AA)
        cv2.line(front_frame, (x[i], frame.shape[0]), (x[i], left_y[i]), bar_color[i], thickness=size, lineType=cv2.LINE_AA)

    blended_frame = blend_frame(front_frame, glow_frame)
    blended_frame = blend_frame(blended_frame, back_frame)
    frame[:] = blended_frame

def draw_disk_bars(frame, spectrum, n_bars, bar_color, option):
    mid_point = frame.shape[0] // 2
    col_width = frame.shape[1] // n_bars
    bar_width = col_width * 3 // 5

    x = np.array(range(n_bars)) * col_width + col_width // 2
    y1 = mid_point - (spectrum * mid_point).astype(int)
    y2 = mid_point + (spectrum * mid_point).astype(int)

    disk_radius_in_bar = math.ceil(option['radius'] / col_width)

    if (option['glow'] != 0):
        glow_strength = option['glow']

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            pixel_m = pixel_mask(alpha, option['mp4'])
            glow_color = [((c * pixel_m)) for c in bar_color]
            for i in range(n_bars // 2 - disk_radius_in_bar):
                cv2.line(frame, (x[i], y1[i]), (x[i], y2[i]), glow_color[i], thickness=option['size'] + j, lineType=cv2.LINE_AA)
            for i in range(n_bars // 2 + disk_radius_in_bar, n_bars):
                cv2.line(frame, (x[i], y1[i]), (x[i], y2[i]), glow_color[i], thickness=option['size'] + j, lineType=cv2.LINE_AA)

    for i in range(n_bars // 2 - disk_radius_in_bar):
        cv2.line(frame, (x[i], y1[i]), (x[i], y2[i]), bar_color[i], thickness=bar_width, lineType=cv2.LINE_AA)

    for i in range(n_bars // 2 + disk_radius_in_bar, n_bars):
        cv2.line(frame, (x[i], y1[i]), (x[i], y2[i]), bar_color[i], thickness=bar_width, lineType=cv2.LINE_AA)

    if (option['glow'] != 0):
        for i in range(n_bars // 2 - disk_radius_in_bar):
            cv2.line(frame, (x[i], y1[i]), (x[i], y2[i]), (255, 255, 255, 255), thickness=option['size'] // 10 + 1, lineType=cv2.LINE_AA)
        for i in range(n_bars // 2 + disk_radius_in_bar, n_bars):
            cv2.line(frame, (x[i], y1[i]), (x[i], y2[i]), (255, 255, 255, 255), thickness=option['size'] // 10 + 1, lineType=cv2.LINE_AA)

    disk_frame = np.zeros_like(frame)
    disk_image = option['image']
    rotation_angle = 720 * np.mean(spectrum)
    rotated_circular_img = rotate_image(disk_image, rotation_angle)
    center_x = frame.shape[1] // 2
    center_y = frame.shape[0] // 2
    if (option['glow'] != 0):
        glow_strength = int(option['glow'] * (1.0 + 5 * np.mean(spectrum)))

        # Draw the glow effect by drawing thicker lines with reduced opacity
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            glow_color = list(bar_color[len(bar_color) // 2])
            glow_color[3] = int(glow_color[3] * alpha)
            cv2.circle(disk_frame, (center_x, center_y), option['radius'] + j // 2, glow_color, thickness=cv2.FILLED, lineType=cv2.LINE_AA)
    overlay_image_alpha(disk_frame, rotated_circular_img, center_x - option['radius'], center_y - option['radius'])
    blended_frame = blend_frame(disk_frame, frame)
    frame[:] = blended_frame

def draw_sharp_circular_liquid(frame, spectrum, color_mode, option):
    frame_number = option['frame_number']
    size = option['size']
    rotation_speed = 0.5
    
    inner_circle_radius = frame.shape[0] // 6
    center_x, center_y = frame.shape[1] // 2, frame.shape[0] // 2
    max_bar_length = min(center_x, center_y) - inner_circle_radius
    rotation_angle = (frame_number * rotation_speed) % 360

    y = spectrum * frame.shape[0] // 2
    n_bars = len(y)
    y = np.concatenate([y[n_bars *  5 // 12:n_bars *7 // 12] / 2, y[0:n_bars * 1 // 6], y[n_bars *  5 // 12:n_bars *7 // 12] / 2, y[n_bars * 5 // 6::], y[n_bars *  5 // 12:n_bars *7 // 12] / 2])
    x = np.linspace(0, frame.shape[1], len(y))

    coefficients = np.polyfit(x, y, 10)
    polynomial = np.poly1d(coefficients)

    smooth_factor = 600
    x = np.linspace(0, frame.shape[1] - 1, smooth_factor).astype(int)
    y = abs(polynomial(x).astype(int))

    bar_length = np.interp(y, (0, min(center_x, center_y)), (0, max_bar_length))
    bar_length = bar_length[50:550]
    bar_color = [get_bar_color(i, len(x), color_mode) for i in range(len(bar_length))]

    iter = np.array(range(len(bar_length)))
    angle = (180 / len(bar_length)) * iter + rotation_angle
    mirrored_angle = rotation_angle - (180 / len(bar_length)) * iter

    end_x = (center_x + (inner_circle_radius + bar_length) * np.cos(np.radians(angle))).astype(int)
    end_y = (center_y + (inner_circle_radius + bar_length) * np.sin(np.radians(angle))).astype(int)

    mirrored_end_x = (center_x + (inner_circle_radius + bar_length) * np.cos(np.radians(mirrored_angle))).astype(int)
    mirrored_end_y = (center_y + (inner_circle_radius + bar_length) * np.sin(np.radians(mirrored_angle))).astype(int)

    for i in range(len(bar_length)):
        cv2.circle(frame, (end_x[i], end_y[i]), size, bar_color[i], thickness=cv2.FILLED, lineType=cv2.LINE_AA)
        cv2.circle(frame, (mirrored_end_x[i], mirrored_end_y[i]), size, bar_color[i], thickness=cv2.FILLED, lineType=cv2.LINE_AA)

def draw_symmetric_bars(frame, spectrum, n_bars, color_mode, option):
    center_x = frame.shape[1] // 2
    center_y = frame.shape[0] // 2
    image = option['image'] 
    scale_factor = 0.3  # Tỉ lệ thu nhỏ hình ảnh
    new_size = (int(image.shape[1] * scale_factor), int(image.shape[0] * scale_factor))
    scaled_image = cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)
    image_center_x = scaled_image.shape[1] // 2
    image_center_y = scaled_image.shape[0] // 2
    row_height = frame.shape[0] // n_bars
    bar_height = row_height * 3 // 5
    x_left = center_x - image_center_x
    x_right = center_x + image_center_x

    y_start = center_y - image_center_y
    y_end = center_y + image_center_y
    y_positions = np.array(range(n_bars)) * row_height + row_height // 2
    y_positions = y_positions[(y_positions >= y_start) & (y_positions <= y_end)]
    n_bars = len(y_positions)
    widths = spectrum * frame.shape[1] // 2  # Scale hình lớn hơn để nằm giữa
    widths = widths.astype(int)

    # Convert color_mode to a valid color value
    bar_color = [get_bar_color(i, n_bars, color_mode) for i in range(n_bars)]

    # Vẽ line ngắn lại theo option["bar_height"]
    for i in range(n_bars):
        # Vẽ line bên trái của center
        left_end = x_left - widths[i] // option["bar_height"]
        if left_end > x_left:
            left_end = x_left
        cv2.line(frame, (x_left, y_positions[i]), (left_end, y_positions[i]), bar_color[i], thickness=option["size"], lineType=cv2.LINE_AA)
        # Vẽ line bên phải của center
        right_end = x_right + widths[i] // option["bar_height"]
        if right_end < x_right:
            right_end = x_right
        cv2.line(frame, (x_right, y_positions[i]), (right_end, y_positions[i]), bar_color[i], thickness=option["size"], lineType=cv2.LINE_AA)

    # Add glow effect if enabled
    if option.get('glow', 0) != 0:
        glow_strength = option['glow']
        for j in range(glow_strength - 5, 0, -5):
            alpha = (glow_strength - j) / (glow_strength * 2)
            for i in range(n_bars):
                left_end = max(center_x - widths[i], 0)
                right_end = min(center_x + widths[i], frame.shape[1])
                cv2.line(frame, (center_x, y_positions[i]), (left_end, y_positions[i]), (255, 255, 255, 255), thickness=bar_height // 10 + 1, lineType=cv2.LINE_AA)
                cv2.line(frame, (center_x, y_positions[i]), (right_end, y_positions[i]), (255, 255, 255, 255), thickness=bar_height // 10 + 1, lineType=cv2.LINE_AA)
    
    overlay_image_alpha(frame, scaled_image, center_x - image_center_x, center_y - image_center_y)

def draw_waveform(frame, spectrum, n_bars, color_mode, waveform_type, option):
    match waveform_type:
        case "bars": 
            bar_color = [get_bar_color(i, n_bars, color_mode) for i in range(n_bars)]
            draw_bars(frame, spectrum, n_bars, bar_color, option)
        case "up_bars": 
            bar_color = [get_bar_color(i, n_bars, color_mode) for i in range(n_bars)]
            draw_up_bars(frame, spectrum, n_bars, bar_color, option)
        case "line": 
            bar_color = [get_bar_color(i, n_bars, color_mode) for i in range(n_bars)]
            draw_line(frame, spectrum,bar_color, option)
        case "tiles":
            draw_tiles(frame, spectrum, n_bars, color_mode, option)
        case "tiles_with_cap":  
            draw_tiles_with_cap(frame, spectrum, n_bars, color_mode, option)
        case "revert_tiles":
            draw_revert_tiles(frame, spectrum, n_bars, color_mode, option)
        case "revert_tiles_with_cap":
            draw_revert_tiles_with_cap(frame, spectrum, n_bars, color_mode, option)
        case "circular_bars":
            bar_color = [get_bar_color(i, n_bars, color_mode) for i in range(n_bars)]
            draw_circular_bars(frame, spectrum, n_bars, bar_color, option)
        case "signal":
            bar_color = [get_bar_color(i, n_bars, color_mode) for i in range(n_bars)]
            draw_signal(frame, spectrum,bar_color, option)
        case "string":
            draw_string(frame, spectrum, color_mode, option)
        case "oval":
            bar_color = [get_bar_color(i, n_bars, color_mode) for i in range(n_bars)]
            draw_oval(frame, spectrum, n_bars, bar_color, option)
        case "liquid":
            draw_liquid(frame, spectrum, color_mode, option)
        case "circular_liquid":
            draw_circular_liquid(frame, spectrum, color_mode, option)
        case "circular_signal":
            draw_circular_signal(frame, spectrum, color_mode, option)
        case "image":
            draw_image(frame, spectrum, option)
        case "double_liquid":
            draw_double_liquid(frame, spectrum, color_mode, option)
        case "disk_bars":
            bar_color = [get_bar_color(i, n_bars, color_mode) for i in range(n_bars)]
            draw_disk_bars(frame, spectrum, n_bars, bar_color, option)
        case "sharp_circular_liquid":
            draw_sharp_circular_liquid(frame, spectrum, color_mode, option)
        case "symmetric_bars":
            draw_symmetric_bars(frame, spectrum, n_bars, color_mode, option)

