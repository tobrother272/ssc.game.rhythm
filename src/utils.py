import colorsys
import numpy as np
from scipy.interpolate import interp1d

def color_from_hue(hue):
    color = colorsys.hsv_to_rgb(hue, .8, 1)
    return tuple(int(c * 255) for c in color[::-1])

def hex_to_hsv(hex_color):
    # Strip the '#' character if it exists
    hex_color = hex_color.lstrip('#')

    # Convert hex to RGB
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)

    # Normalize RGB values to [0, 1]
    r, g, b = r / 255.0, g / 255.0, b / 255.0

    # Convert RGB to HSV
    h, s, v = colorsys.rgb_to_hsv(r, g, b)

    return h * 180, s * 255, v * 255

def hex_to_hue(hex_color):
    h, s, v = hex_to_hsv(hex_color)

    # Hue is given in the range [0, 1], we can convert it to degrees
    hue_degrees = h

    return hue_degrees

def get_bar_color(i, n_bars, color_mode, start_color=None, end_color=None):
    a = 255
    if color_mode[0] == "#":
        r, g, b = tuple(int(color_mode[i:i + 2], 16) for i in (5, 3, 1))
        return r, g, b, a
    elif color_mode == 'gradient':
        if start_color and end_color:
            start_color = hex_to_rgb(start_color)  # Chuyển đổi hex thành RGB
            end_color = hex_to_rgb(end_color)      # Chuyển đổi hex thành RGB
            r, g, b = color_gradient(start_color, end_color, n_bars, i)
        else:
            r, g, b = color_from_hue(i / n_bars)
        return r, g, b, a
    else:
        raise ValueError("Unknown color mode")

def hex_to_rgb(hex_color):
    """
    Chuyển đổi chuỗi hex thành tuple RGB.
    """
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))

def color_gradient(start_color, end_color, num_steps, step):
    """
    Interpolate between two colors.
    """
    delta = (np.array(end_color) - np.array(start_color)) / (num_steps - 1)
    return tuple(np.array(start_color) + delta * step)

def find_top_n_max_indices(arr, n):
    if len(arr) < n:
        raise ValueError("Array must contain at least n elements.")
    
    arr_copy = arr.copy()
    max_indices = []

    # Calculate the start index from the middle
    middle_index = len(arr) // 2
    if len(arr) % 2 == 1:
        middle_index += 1

    for _ in range(n):
        # Find the index of the maximum element starting from the middle
        max_index = middle_index + np.argmax(arr_copy[middle_index:])
        if max_index == middle_index:
            max_index = np.argmax(arr_copy[:middle_index])
        max_indices.append(max_index)
        arr_copy[max_index] = 0  # Temporarily set the maximum element to a very small value

    return np.array(max_indices)


def hillarize(x, peek):
    return np.exp(-(x - peek) ** 2 / (2 * 150 ** 2))

def hillarize_sin(points):
    """
    Tạo một đường cong sin dựa trên một chuỗi các điểm.
    
    :param x_values: Danh sách các giá trị đầu vào.
    :param points: Danh sách các điểm (x, y) để nội suy.
    :return: Danh sách các điểm (x, y) đã được nội suy và áp dụng hàm sin.
    """
    points = sorted(points)  # Đảm bảo các điểm được sắp xếp theo trục x
    xs, ys = zip(*points)
    
    # Tạo hàm sin dựa trên các điểm y
    y_sin = np.sin(np.pi * np.array(ys))
    
    # Trả về danh sách các điểm (x, y)
    return list(zip(xs, y_sin))