import os
import argparse
import pysrt
    
def main(args):
    folder_path = args.folder
    output_file = args.output

    srt_files = [f for f in os.listdir(folder_path) if f.endswith('.srt')]
    srt_files.sort(key=lambda x: int(x.split('.')[0]))  # Assuming files are named as 1.srt, 2.srt, etc.
    
    merged_subtitles = pysrt.SubRipFile()
    cumulative_offset = 0
    subtitle_id_counter = 1

    for srt_file in srt_files:
        file_path = os.path.join(folder_path, srt_file)
        subtitles = pysrt.open(file_path)

        for subtitle in subtitles:
            subtitle.shift(seconds=cumulative_offset / 1000)
            subtitle.index = subtitle_id_counter
            merged_subtitles.append(subtitle)
            subtitle_id_counter += 1

        if subtitles:
            end_offset = int(srt_file.split('.')[1]) * 1000
            cumulative_offset += end_offset

    merged_subtitles.save(output_file, encoding='utf-8')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Subscript merge tool')
    parser.add_argument('-f', '--folder', type=str, help='Folder containing the subtitle files')
    parser.add_argument('-o', '--output', type=str, help='Output file path')

    args = parser.parse_args()
    main(args)