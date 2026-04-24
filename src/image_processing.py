import cv2
import numpy as np

def load_and_crop_image(image_path, output_size=None):
    # Load the image
    img = cv2.imread(image_path, cv2.IMREAD_COLOR)

    # Determine the smaller dimension (width or height) of the image
    min_dim = min(img.shape[:2])

    # Create a circular mask centered in the middle of the square crop
    mask = np.zeros((min_dim, min_dim), dtype=np.uint8)
    cv2.circle(mask, (min_dim // 2, min_dim // 2), min_dim // 2, (255), thickness=-1)

    # Calculate the cropping square to extract the circle
    x = (img.shape[1] - min_dim) // 2
    y = (img.shape[0] - min_dim) // 2

    # Crop the image to the square
    cropped_img = img[y:y + min_dim, x:x + min_dim]

    # Apply the circular mask
    circular_img = cv2.bitwise_and(cropped_img, cropped_img, mask=mask)

    # Create an alpha channel based on the mask
    alpha_channel = mask.copy()

    # Combine the cropped image with the alpha channel
    circular_img_with_alpha = cv2.merge((circular_img, alpha_channel))

    # If an output size is specified, resize the image
    if output_size:
        circular_img_with_alpha = cv2.resize(circular_img_with_alpha, (output_size, output_size), interpolation=cv2.INTER_AREA)

    return circular_img_with_alpha

def overlay_image_alpha(img, img_overlay, x, y):
    # Image ranges
    y1, y2 = max(0, y), min(img.shape[0], y + img_overlay.shape[0])
    x1, x2 = max(0, x), min(img.shape[1], x + img_overlay.shape[1])

    # Overlay ranges
    y1o, y2o = max(0, -y), min(img_overlay.shape[0], img.shape[0] - y)
    x1o, x2o = max(0, -x), min(img_overlay.shape[1], img.shape[1] - x)

    if y1 >= y2 or x1 >= x2 or y1o >= y2o or x1o >= x2o:
        return

    if img_overlay.shape[2] == 4:
        alpha_overlay = img_overlay[y1o:y2o, x1o:x2o, 3] / 255.0
        alpha_background = 1 - alpha_overlay

        for c in range(0, 4):
            img[y1:y2, x1:x2, c] = (alpha_overlay * (img_overlay[y1o:y2o, x1o:x2o, c] / 1.0) +
                                    alpha_background * (img[y1:y2, x1:x2, c] / 1.0))
    else:
        for c in range(0, 3):
            img[y1:y2, x1:x2, c] = img_overlay[y1o:y2o, x1o:x2o, c] 
        
def rotate_image(image, angle):
    """ Rotate the image by the given angle. """
    # Find the center of the image to set the rotation axis
    center = (image.shape[1] // 2, image.shape[0] // 2)

    # Compute the rotation matrix
    rotation_matrix = cv2.getRotationMatrix2D(center, angle, scale=1.0)

    # Perform the rotation
    rotated_image = cv2.warpAffine(image, rotation_matrix, (image.shape[1], image.shape[0]))

    return rotated_image

def fit_image(image, target_size):
    """ Resize the image to fit within the target_size. """
    # Get the dimensions of the image
    image_size = image.shape[:2]

    # Get the scaling factor for each dimension
    scale = min(target_size[0] / image_size[1], target_size[1] / image_size[0])

    # Calculate the new size of the image
    new_size = (int(image_size[1] * scale), int(image_size[0] * scale))

    # Resize the image
    resized_image = cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)

    return resized_image

def zoom_image(image, percent): 
    """ Zoom in or out of the image by the given percentage. """
    # Get the dimensions of the image
    image_size = image.shape[:2]

    # Calculate the new size of the image
    new_size = (int(image_size[1] * percent), int(image_size[0] * percent))

    # Resize the image
    resized_image = cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)
    

    return resized_image

def blend_frame(frame1, frame2):
    # Extract the RGB and alpha channels from both frames
    rgb1 = frame1[..., :3]
    alpha1 = frame1[..., 3:4] / 255.0

    rgb2 = frame2[..., :3]
    alpha2 = frame2[..., 3:4] / 255.0

    # Blend the RGB channels
    out_rgb = rgb1 * alpha1 + rgb2 * alpha2 * (1 - alpha1)

    # Blend the alpha channels
    out_alpha = alpha1 + alpha2 * (1 - alpha1)

    # Avoid division by zero in case both alphas are 0
    combined_alpha = np.maximum(out_alpha, 1e-10)
    
    # Normalize the blended RGB by the combined alpha to avoid darkening the output
    out_rgb /= combined_alpha

    # Combine the blended RGB channels with the blended alpha channel
    blended_frame = np.concatenate((out_rgb, out_alpha * 255), axis=-1).astype(np.uint8)

    return blended_frame

def pixel_mask(alpha, mp4):
    if mp4:
        return np.array([alpha, alpha, alpha, 0])
    else:
        return np.array([1, 1, 1, alpha])