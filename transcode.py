#!/usr/bin/python3

import os
import re
import sys
import yaml
import subprocess
from queue import Queue
from threading import Thread

DEFAULT_CONFIG = os.path.expanduser('~/.transcode.yml')

valid_predicates = ['vcodec', 'res_height', 'res_width', 'runtime', 'source_size', 'fps']
video_re = re.compile('^.*Duration: (\d+):(\d+):.* Stream #0:0.*: Video: (\w+).*, (\d+)x(\d+).* (\d+) fps,.*$', re.DOTALL)
thread_queue = Queue(10)
complete = set()
queue_path = None
profiles = dict()
matching_rules = dict()
config = dict()
concurrent_jobs = 2
keep_source = False
dry_run = False


class MediaInfo:

    def __init__(self, path, vcodec, res_height, res_width, runtime, source_size, fps):
        self._path = path
        self._vcodec = vcodec
        self._res_height = res_height
        self._res_width = res_width
        self._runtime = runtime
        self._filesize_mb = source_size
        self._fps = fps

    @property
    def path(self):
        return self._path

    @property
    def vcodec(self):
        return self._vcodec

    @property
    def res_height(self):
        return self._res_height

    @property
    def res_width(self):
        return self._res_width

    @property
    def runtime(self):
        return self._runtime

    @property
    def filesize_mb(self):
        return self._filesize_mb

    @property
    def fps(self):
        return self._fps


def match_profile(path, mediainfo) -> (str, str):
    for description, body in matching_rules.items():
        if 'rules' not in body:
            # no rules section, match by default
            return body['profile'], description
        for pred, value in body['rules'].items():
            inverted = False
            if pred not in valid_predicates:
                print(f'Invalid predicate {pred} in rule {description}')
                exit(1)
            if isinstance(value, str) and len(value) > 1 and value[0] == '!':
              inverted = True
              value = value[1:]
            if pred == 'vcodec' and mediainfo.vcodec != value and not inverted:
                break
            if pred == 'path':
                try:
                    m = re.match(mediainfo.path, value)
                    if m is None:
                        break
                except Exception as ex:
                    print(f'invalid regex {mediainfo.path} in rule {description}')
                    exit(0)
            if pred == 'res_height' and len(value) > 1:
                if value.isnumeric():
                    value = '==' + value  # make python-friendly
                if not eval(f'{mediainfo.res_height}{value}'):
                    break
            if pred == 'res_width' and len(value) > 1:
                if value.isnumeric():
                    value = '==' + value  # make python-friendly
                if not eval(f'{mediainfo.res_width}{value}'):
                    break
            if pred == 'runtime' and len(value) > 1:
                if value.isnumeric():
                    value = '==' + value  # make python-friendly
                if not eval(f'{mediainfo.runtime}{value}'):
                    break
            if pred == 'fps' and len(value) > 1:
                if value.isnumeric():
                    value = '==' + value  # make python-friendly
                if not eval(f'{mediainfo.fps}{value}'):
                    break
            if pred == 'source_size' and len(value) > 1:
                if value.isnumeric():
                    value = '==' + value  # make python-friendly
                if not eval(f'{mediainfo.filesize_mb}{value}'):
                    break
        else:
            # didn't bail out on any predicates, have a match
            return body['profile'], description
    return None, None


def loadq(queuepath) -> list:
    if not os.path.exists(queuepath):
        print(f'Queue file {queuepath} not found')
        return []
    with open(queuepath, 'r') as qf:
        _files = [fn.rstrip() for fn in qf.readlines()]
        return _files


def fetch_details(_path) -> MediaInfo:
    with subprocess.Popen(['ffmpeg', '-i', path], stderr=subprocess.PIPE) as proc:
        output = proc.stderr.read().decode(encoding='utf8')
        match = video_re.match(output)
        if match is None or len(match.groups()) != 6:
            print(f'>>>> regex match on video stream data failed: ffmpeg -i {_path}')
            return MediaInfo(path, None, 0, 0, 0, 0, 0)
        else:
            _dur_hrs, _dur_mins, _codec, _res_width, _res_height, fps = match.group(1, 2, 3, 4, 5, 6)
            filesize = os.path.getsize(path) / (1024 * 1024)
            return MediaInfo(_path, _codec, int(_res_width), int(_res_height), (int(_dur_hrs) * 60) + int(_dur_mins), filesize, int(fps))


def perform_transcodes():
    global keep_source, config, dry_run

    while not thread_queue.empty():
        try:
            _inpath, _outpath, profile_name = thread_queue.get()
            print(f'transcoding {_inpath}:')
            _profile = profiles[profile_name]
            oinput = _profile['input_options'].split()
            ooutput = _profile['output_options'].split()
            cli = [config['ffmpeg']] + oinput + ['-i', _inpath] + ooutput + [_outpath]
            # cli = [FFMPEG, '-hide_banner', '-nostats', '-hwaccel', 'cuvid', '-i', _inpath, '-c:v', 'hevc_nvenc',
            #       '-profile:v', 'main', '-preset', 'medium', '-crf', '22', '-c:a', 'copy', '-c:s', 'copy', '-f',
            #       'matroska',
            #       _outpath]
            print(profile_name + ' -->  ' + ' '.join(cli) + '\n')
            if dry_run:
                continue
            p = subprocess.Popen(cli)
            p.wait()
            if p.returncode == 0:
                complete.add(_inpath)
                if not keep_source:
                    print('removing ' + _inpath)
                    os.remove(_inpath)
                    print('renaming ' + _outpath)
                    os.rename(_outpath, _outpath[:-4])
            else:
                print('error during transcode, .tmp file removed')
                os.remove(_outpath)
        finally:
            thread_queue.task_done()


def load_config(_path):
    global profiles, matching_rules, config, concurrent_jobs

    with open(_path, 'r') as f:
        yml = yaml.load(f)
        profiles = yml['profiles']
        matching_rules = yml['rules']
        config = yml['config']
        concurrent_jobs = config['concurrent_jobs']


if __name__ == '__main__':

    if len(sys.argv) == 2 and sys.argv[1] == '-h':
        print('usage: {} [OPTIONS]'.format(sys.argv[0], ))
        print('  or   {} [OPTIONS] --from-file <filename>'.format(sys.argv[0], ))
        print('  or   {} [OPTIONS] file ...'.format(sys.argv[0], ))
        print('No parameters indicates to process the default queue files using profile matching rules.')
        print(
            'The --from-file filename is a file containing a list of full paths to files for transcoding. ' +
            'If full paths not used, defaults to current directory')
        print('OPTIONS:')
        print('  -s         Process files sequentially even if configured for multiple concurrent jobs')
        print('  --dry-run  Run without actually transcoding or modifying anything, useful to test rules and profiles')
        print(
            '  -k         Keep source files after transcoding. If used, the transcoded file will have the same name and .tmp extension')
        print('  -y <file>  Full path to configuration file.  Default is ~/.transcode.yml')
        print('  -p         profile to use. If used with --from-file, applies to all listed media in <filename>')
        print('             Otherwise, applies to all following files up to the next occurrance')
        print(
            '                 Ex: {} --from-file /home/me/batch.txt -p hevc_hd /tmp/testvid1.mp4 /tmp/testvid2.mp4'.format(
                sys.argv[0]))
        print(
            '                   This will transcode all videos listed in batch.txt using the rules, using hevc_hd profile for the others')
        print('                 Ex: {} -p hevc_25fps --from-file /home/me/batch.txt'.format(sys.argv[0]))
        print('                   This will transcode all videos listed in batch.txt using the the hevc_25fps profile')
        print('                 Ex: {} -p hevc_25fps /tmp/vid1.mp4 -p hevc_hd /tmp/vid2.mp4'.format(sys.argv[0]))
        print('                   This will transcode the given videos using different profiles')
        print('Individual files may be listed on the command line for processing\n')
        sys.exit(0)

    files = list()
    profile = None
    queue_path = None
    if len(sys.argv) > 1:
        files = []
        arg = 1
        while arg < len(sys.argv):
            if sys.argv[arg] == '--from-file':
                queue_path = sys.argv[arg + 1]
                arg += 1
                tmpfiles = loadq(queue_path)
                files.extend([(f, profile) for f in tmpfiles])
            elif sys.argv[arg] == '-p':
                profile = sys.argv[arg + 1]
                arg += 1
            elif sys.argv[arg] == '-y':
                arg += 1
                load_config(sys.argv[arg])
            elif sys.argv[arg] == '-s':
                concurrent_jobs = 1
            elif sys.argv[arg] == '-k':
                keep_source = True
            elif sys.argv[arg] == '--dry-run':
                dry_run = True
            else:
                files.append((sys.argv[arg], profile))
            arg += 1

    if len(profiles) == 0:
        load_config(DEFAULT_CONFIG)

    if len(files) == 0 and queue_path is None and 'default_queue_file' in config:
        queue_path = config['default_queue_file']
        tmpfiles = loadq(queue_path)
        files.extend([(f, profile) for f in tmpfiles])

    if files is None:
        exit(0)

    for path, forced_profile in files:
        #
        # do some prechecks...
        #
        if forced_profile is not None and forced_profile not in profiles:
            print(f'profile "{forced_profile}" referenced from command line not found')
            exit(1)

        if len(path) == 0:
            continue
        if not os.path.isfile(path):
            print('path not found, skipping: ' + path)
            continue

        path = os.path.abspath(path)	# convert to full path so that rule filtering can work
        print('processing ' + path)
        minfo = fetch_details(path)
        if minfo.vcodec is not None:
            if forced_profile is None:
                matched_profile, rule = match_profile(path, minfo)
                if matched_profile is None:
                    print(f'No matching profile found - skipped')
                    continue
                if matched_profile.upper() == 'SKIP':
                    print(f'Skipping due to profile rule: {rule}')
                    complete.add(path)
                    continue
                if matched_profile not in profiles:
                    print(f'profile "{matched_profile}" referenced from rule "{rule}" not found')
                    exit(1)
                the_profile = profiles[matched_profile]
                outpath = path[0:path.rfind('.')] + the_profile['extension'] + '.tmp'
                thread_queue.put((path, outpath, matched_profile))
            else:
                #
                # looks good, add this file to the thread queue
                #
                the_profile = profiles[forced_profile]
                outpath = path[0:path.rfind('.')] + the_profile['extension'] + '.tmp'
                thread_queue.put((path, outpath, forced_profile))

            # if vcodec in ('hevc', 'x265', 'h265'):
            #     print('found h265, skipping: ' + path)
            #     complete.add(path)
            #     continue
            # if int(res_height) < 720:
            #     print('low resolution video will not be transcoded')
            #     complete.add(path)
            #     continue

    print()
    #
    # all files are listed in the queue so start the threads
    #
    jobs = list()
    concurrent_jobs = min(concurrent_jobs, thread_queue.qsize())
    for _ in range(concurrent_jobs):
        t = Thread(target=perform_transcodes)
        jobs.append(t)
        t.start()

    # wait for all jobs to complete
    thread_queue.join()

    if not dry_run and queue_path is not None:
        # pick up any newly added files
        files = set(loadq(queue_path))
        files = files - complete
        if len(files) > 0:
            with open(queue_path, 'w') as f:
                for path in files:
                    f.write(path + '\n')
        else:
            os.remove(queue_path)

    if 'plex_server' in config and config['plex_server'] is not None and not dry_run:
        try:
            from plexapi.server import PlexServer

            plex = PlexServer('http://{}'.format(config['plex_server']))
            plex.library.update()
            # plex.library.section(PLEX_DEFAULT_REFRESH_LIBRARY).update()
        except Exception as ex:
            print(
                'Library not installed. To use Plex notifications please install the Python 3 Plex API ' +
                '("pip3 install plexapi")')

SAMPLE_YAML = """
##
# global configuration
##
config:
  default_queue_file: '/volume1/config/sonarr/transcode_queue.txt'
  ffmpeg: '/usr/bin/ffmpeg'
  concurrent_jobs: 2
  plex_server: null 

##
# profile definitions
##

profiles:
  hevc_hd_preserved:          # default for almost everything
      input_options: |
        -hide_banner
        -nostats
        -hwaccel cuvid
      output_options: |
        -c:v hevc_nvenc
        -profile:v main
        -preset medium
        -crf 20
        -c:a copy
        -c:s copy
        -f matroska

  hevc_hd_25fps:               # when movie source is just too big, cut down fps
      input_options: |
         -hide_banner
         -nostats
         -hwaccel cuvid
      output_options: |
        -c:v hevc_nvenc
        -profile:v main
        -preset medium
        -crf 20
        -c:a copy
        -c:s copy
        -f matroska
        -r 25

  hevc_hd_lq:                 # lower quality, for when source material isn't that good anyhow
      input_options: |
         -hide_banner
         -nostats
         -hwaccel cuvid
      output_options: |
        -c:v hevc_nvenc
        -profile:v main
        -preset medium
        -crf 23
        -c:a copy
        -c:s copy
        -f matroska


#
# Automatching happens when a profile isn't provided on the command line.  These rules are evalulated to find the
# most appropriate profile for each video to be transcoded.
#
# rule predicates:
#
#  Use:
#      predicate: value
#
#  Where:
#
#      predicate        one of the supported values:
#                           vcodec         Video codec of the source ('ffmpeg -codecs' to see full list)
#                           res_height     Source video resolution height, operators < and > allowed
#                           res_width      Source video resolution width, operators < and > allowed
#                           source_size    Size of the source file (in megabytes), operators allowed
#                           runtime        Source runtime in minutes, operators allowed
#                           path           Full path of the source file. Value can be a regular expression.
#      value            what to match the predicate against. Simple values are equality tests but numerics can also have '<' or '>'
#
# Rules are evaluated in order.  First matching rule wins so order wisely.
# Rules with a profile of "SKIP" mean to skip processing of the matched video
#
rules:
  'skip video if already encoded in hevc/h265':
      profile: SKIP
      rules:
        codec: 'hevc'

  'skip video if resolution < 720 (do not bother transcoding)':
      profile: SKIP
      rules:
        res_height: '<720'

  'for content I consider too big for their runtime':
      profile: hevc_hd_25fps
      rules:
        runtime:      '<180'      # less than 3 hours
        source_size:  '>5000'  # ..and larger than 5 gigabytes

  'default':    # this will be the DEFAULT (no rules implies a match)
      profile: hevc_hd_preserved

"""