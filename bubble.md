# Sóng Bubble

Tạo các bóng nhỏ chóp bay lên và tăng tốc khi có bass mạnh

Các tham số:
- `-W <width>`: chiều rộng video
- `-H <height>`: chiều cao video
- `-i <path_to_input>`: đường dẫn tới file audio
- `-o <path_to_output_folder>`: đường dẫn tới thư mục lưu trữ video
- `-d <duration>`: thời lượng video (giây), mặc định là cả bài
- `--mindot <value>`: Số lượng bóng nhỏ nhất
- `--maxdot <value>`: Số lượng bóng lớn nhất
- `--a <audio>`: có âm thanh hay không, mặc định là không
- `-t <token>`: token
- `-u <url>`: url

## Cách sử dụng
```bash
python src/bubble.py -W 1280 -H 720 -i demos/song.mp3 -o demos/output -d 12 -a 1 -t "315|aAyKUOBMXiw64bo9XarWu31y5MdRoPBMg0SwroA8" -u user.sscapi.co
```
