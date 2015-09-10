#!/usr/bin/env python

import os.path
import os
import re
import signal
from urllib3.util import parse_url
from subprocess import Popen, PIPE
import logging
import locale
import json

logger = logging.getLogger(__name__)

console_encoding = locale.getdefaultlocale()[1] or 'UTF-8'


class FFMpegError(Exception):
    pass


class FFMpegConvertError(Exception):
    def __init__(self, message, cmd, output, details=None, pid=0):
        """
        @param    message: Error message.
        @type     message: C{str}

        @param    cmd: Full command string used to spawn ffmpeg.
        @type     cmd: C{str}

        @param    output: Full stdout output from the ffmpeg command.
        @type     output: C{str}

        @param    details: Optional error details.
        @type     details: C{str}
        """
        super(FFMpegConvertError, self).__init__(message)

        self.cmd = cmd
        self.output = output
        self.details = details
        self.pid = pid

    def __repr__(self):
        error = self.details if self.details else self.message
        return ('<FFMpegConvertError error="%s", pid=%s, cmd="%s">' %
                (error, self.pid, self.cmd))

    def __str__(self):
        return self.__repr__()


class FFMpeg(object):
    """
    FFMPeg wrapper object, takes care of calling the ffmpeg binaries,
    passing options and parsing the output.

    >>> f = FFMpeg()
    """
    DEFAULT_JPEG_QUALITY = 4
    AUDIO_PEAK_MAX = -1  # dBTP
    AUDIO_LOUDNESS_TARGET = -16  # LUFS

    def __init__(self, ffmpeg_path=None, ffprobe_path=None):
        """
        Initialize a new FFMpeg wrapper object. Optional parameters specify
        the paths to ffmpeg and ffprobe utilities.
        """

        def which(name):
            path = os.environ.get('PATH', os.defpath)
            for d in path.split(':'):
                fpath = os.path.join(d, name)
                if os.path.exists(fpath) and os.access(fpath, os.X_OK):
                    return fpath
            return None

        if ffmpeg_path is None:
            ffmpeg_path = 'ffmpeg'

        if ffprobe_path is None:
            ffprobe_path = 'ffprobe'

        if '/' not in ffmpeg_path:
            ffmpeg_path = which(ffmpeg_path) or ffmpeg_path
        if '/' not in ffprobe_path:
            ffprobe_path = which(ffprobe_path) or ffprobe_path

        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

        if not os.path.exists(self.ffmpeg_path):
            raise FFMpegError("ffmpeg binary not found: " + self.ffmpeg_path)

        if not os.path.exists(self.ffprobe_path):
            raise FFMpegError("ffprobe binary not found: " + self.ffprobe_path)

    @staticmethod
    def _spawn(cmds):
        logger.debug('Spawning ffmpeg with command: ' + ' '.join(cmds))
        return Popen(cmds, shell=False, stdin=PIPE, stdout=PIPE, stderr=PIPE,
                     close_fds=True)

    def _check_vob_name(self, source):
        match = re.search('/VIDEO_TS/(VTS_\d\d_)\d(.VOB)$', source, re.IGNORECASE)
        if match is None:
            return source
        base_name = match.group(1)
        extension = match.group(2)
        path = os.path.dirname(source)
        items = os.listdir(path)
        vobs = [os.path.join(path, item) for item in items
                if re.match('{0}[1-9]{1}'.format(base_name, extension), item)]
        if len(vobs) == 1:
            return source
        vobs.sort()
        vobs = '|'.join(vobs)
        return 'concat:{0}'.format(vobs)

    def is_url(self, url):
        #: Accept objects that have string representations.
        try:
            url = unicode(url)
        except NameError:
            # We're on Python 3.
            url = str(url)
        except UnicodeDecodeError:
            pass

        # Support for unicode domain names and paths.
        scheme, auth, host, port, path, query, fragment = parse_url(url)

        if not scheme or not host:
            return False

        # Only want to apply IDNA to the hostname
        try:
            host = host.encode('idna').decode('utf-8')
        except UnicodeError:
            return False

        return True

    def probe(self, fname, posters_as_video=False):
        """
        Examine the media file and determine its format and media streams.
        Returns the MediaInfo object, or None if the specified file is
        not a valid media file.

        >>> info = FFMpeg().probe('test1.ogg')
        >>> info['container']
        'ogg'
        >>> info['duration']
        33.00
        >>> info['video']['codec']
        'theora'
        >>> info['video']['width']
        720
        >>> info['video']['height']
        400
        >>> info['audio']['codec']
        'vorbis'
        >>> info['audio']['channels']
        2
        :param posters_as_video: Take poster images (mainly for audio files) as
            A video stream, defaults to False
        """
        if not os.path.exists(fname) and not self.is_url(fname):
            return None

        p = self._spawn([self.ffprobe_path, '-v', 'quiet', '-print_format',
                        'json', '-show_format', '-show_streams', fname])
        stdout_data, _ = p.communicate()
        stdout_data = stdout_data.decode(console_encoding, 'ignore')
        info = json.loads(stdout_data)

        info['posters'] = []
        if 'streams' in info:
            for stream in info['streams']:
                try:
                    attached_pic = stream['disposition']['attached_pic']
                except KeyError:
                    attached_pic = None
                if stream['codec_type'] not in info:
                    info[stream['codec_type'] + 's'] = []
                    if (stream['codec_type'] != 'video'
                            or (posters_as_video or not attached_pic)):
                        info[stream['codec_type']] = stream
                info[stream['codec_type'] + 's'].append(stream)
                if attached_pic:
                    info['posters'].append(stream)

        def de_under(key, value, branch):
            branch[key.replace('_', '')] = value

        def lower(key, value, branch):
            branch[key] = value.lower()

        def container(key, value, branch):
            info['container'] = value.lower().split(',')

        def extension(key, value, branch):
            info['extension'] = os.path.splitext(value)[1][1:].lower()

        def codec(key, value, branch):
            if 'aac' in value:
                value = 'aac'
            branch['codec'] = value.lower()

        def bitrate_in_kbps(key, value, branch):
            branch['bitrate'] = round(value / 1000.0)

        def bitrate_in_mbps(key, value, branch):
            branch['bitrate'] = round(value / 1000.0 / 1000, 1)

        def fps(key, value, branch):
            branch['fps'] = round(value, 2)

        def level(key, value, branch):
            branch[key] = round(value / 10.0, 1)

        FIELD_MAPPING = {
            'format': {
                'format_name': container,
                'filename': extension,
                'bit_rate': bitrate_in_mbps,
            },
            'audios': {
                'codec_name': codec,
                'sample_rate': de_under,
                'bit_rate': bitrate_in_kbps,
            },
            'videos': {
                'codec_name': codec,
                'avg_frame_rate': fps,
                'bit_rate': bitrate_in_mbps,
                'profile': lower,
                'level': level,
            }
        }

        def clean_info(branch, parent=None):
            if parent is None:
                mapping = FIELD_MAPPING
            else:
                mapping = FIELD_MAPPING.get(parent, {})

            for key, value in branch.items():
                if key in ('streams', 'audio', 'video'):
                    continue

                if isinstance(value, dict):
                    clean_info(value, key)
                    continue

                if isinstance(value, list):
                    for item in value:
                        clean_info(item, key)
                    continue

                if isinstance(value, basestring):
                    try:
                        if '/' in value:
                            n, d = value.split('/', 1)
                            value = float(n) / float(d)
                        elif '.' in value:
                            value = float(value)
                        else:
                            value = int(value)
                    except ZeroDivisionError:
                        value = 0
                    except ValueError:
                        value = value.strip()
                    branch[key] = value

                if key in mapping:
                    mapping[key](key, value, branch)

        clean_info(info)

        # For .VOB file get duration with lsdvd.
        fname = self._check_vob_name(fname)
        if fname.upper().endswith('.VOB'):
            part = fname.rsplit('|', 1)[-1]
            volume, title = part.split('VIDEO_TS/VTS_', 1)
            title = str(int(title.split('_', 1)[0]))
            p = self._spawn(['lsdvd', '-q', '-Oy', '-t', title, volume])
            stdout_data, _ = p.communicate()
            stdout_data = stdout_data.decode(console_encoding, 'ignore')
            try:
                exec stdout_data in locals()
                duration = float(lsdvd['track'][0]['length'])
            except Exception:
                pass
            else:

                def update_duration(data, duration):
                    if isinstance(data, dict):
                        if 'duration' in data:
                            # Keep ffprobe duration if difference with lsdvd
                            # duration is less then 1%.
                            probe_duration = data['duration']
                            gap = 2
                            if isinstance(probe_duration, (float, int)) and probe_duration > 0:
                                if probe_duration > duration:
                                    gap = probe_duration / duration
                                else:
                                    gap = duration / probe_duration
                            if gap > 1.01:
                                data['duration'] = duration
                            data['duration_lsdvd'] = duration
                            data['duration_probe'] = probe_duration

                        for value in data.values():
                            update_duration(value, duration)
                    elif isinstance(data, (tuple, list)):
                        for item in data:
                            update_duration(item, duration)

                update_duration(info, duration)

        return info

    def convert(self, infile, outfile, opts, timeout=10, nice=None, get_output=False):
        """
        Convert the source media (infile) according to specified options
        (a list of ffmpeg switches as strings) and save it to outfile.

        Convert returns a generator that needs to be iterated to drive the
        conversion process. The generator will periodically yield timecode
        of currently processed part of the file (ie. at which second in the
        content is the conversion process currently).

        The optional timeout argument specifies how long should the operation
        be blocked in case ffmpeg gets stuck and doesn't report back. See
        the documentation in Converter.convert() for more details about this
        option.

        >>> conv = FFMpeg().convert('test.ogg', '/tmp/output.mp3',
        ...    ['-acodec libmp3lame', '-vn'])
        >>> for timecode in conv:
        ...    pass # can be used to inform the user about conversion progress

        """
        if not os.path.exists(infile) and not self.is_url(infile):
            raise FFMpegError("Input file doesn't exist: " + infile)

        infile = self._check_vob_name(infile)

        cmds = [self.ffmpeg_path, '-hide_banner']
        # Add duration and position flag before input when we can.
        if '-t' in opts:
            idx = opts.index('-t')
            cmds.append(opts.pop(idx))
            cmds.append(opts.pop(idx))
        if '-ss' in opts and ('-t' in opts or '-to' not in opts):
            idx = opts.index('-ss')
            cmds.append(opts.pop(idx))
            cmds.append(opts.pop(idx))

        cmds.extend(['-i', infile])

        if nice is not None:
            if 0 < nice < 20:
                cmds = ['nice', '-n', str(nice)] + cmds
            else:
                raise FFMpegError("Invalid nice value: {0}".format(nice))

        cmds.extend(opts)
        cmds.extend(['-y', outfile])
        if timeout:
            def on_sigalrm(*_):
                signal.signal(signal.SIGALRM, signal.SIG_DFL)
                raise Exception('timed out while waiting for ffmpeg')

            signal.signal(signal.SIGALRM, on_sigalrm)

        try:
            p = self._spawn(cmds)
        except OSError:
            raise FFMpegError('Error while calling ffmpeg binary')

        yielded = False
        buf = []
        total_output = []
        pat = re.compile(r'time=([0-9.:]+)')

        while True:
            if timeout:
                signal.alarm(timeout)

            ret = p.stderr.read(10)

            if timeout:
                signal.alarm(0)
            if not ret:
                break

            total_output.append(ret)
            buf.append(ret)
            if '\r' in ret:
                buf = ''.join(buf)
                buf = buf.decode(console_encoding, 'ignore')
                line, buf = buf.split('\r', 1)
                buf = [buf]
                tmp = pat.search(line)
                if tmp:
                    timecode = timecode_to_seconds(tmp.group(1))
                    yielded = True
                    yield timecode

        total_output = ''.join(total_output)
        total_output = total_output.decode(console_encoding, 'ignore')
        if not yielded:
            # There may have been a single time, check it
            tmp = pat.search(total_output)
            if tmp:
                timecode = timecode_to_seconds(tmp.group(1))
                yielded = True
                yield timecode

        if timeout:
            signal.signal(signal.SIGALRM, signal.SIG_DFL)

        p.communicate()  # wait for process to exit

        if not total_output:
            raise FFMpegError('Error while calling ffmpeg binary')

        cmd = ' '.join(cmds)
        if '\n' in total_output:
            line = total_output.split('\n')[-2]

            if line.startswith('Received signal'):
                # Received signal 15: terminating.
                raise FFMpegConvertError(line.split(':')[0], cmd, total_output, pid=p.pid)
            if line.startswith(infile + ': '):
                err = line[len(infile) + 2:]
                raise FFMpegConvertError('Encoding error', cmd, total_output,
                                         err, pid=p.pid)
            if line.startswith('Error while '):
                raise FFMpegConvertError('Encoding error', cmd, total_output,
                                         line, pid=p.pid)
            if not yielded:
                raise FFMpegConvertError('Unknown ffmpeg error', cmd,
                                         total_output, line, pid=p.pid)
            if get_output:
                yield total_output
        if p.returncode != 0:
            raise FFMpegConvertError('Exited with code %d' % p.returncode, cmd,
                                     total_output, pid=p.pid)

    def analyze(self, infile, audio_level=True, interlacing=True, crop=False, start=None, duration=None, end=None, timeout=10, nice=None):
        """
        Analyze the video frames to find if the video need to be deinterlaced
        and/or crop to remove black strips.
        Analyze the audio to find if the audio need to be normalize
        and by how much. All analyses are together so FFMpeg can do them
        in the same pass.
        """
        if not audio_level and not interlacing and not crop:
            raise FFMpegError('Nothing selected to analyze (audio level, '
                              'interlacing or crop).')

        opts = ['-f', 'null']
        if audio_level:
            opts.extend(['-af', 'ebur128=peak=true:framelog=verbose'])
        else:
            opts.append('-an')

        video_filters = []
        if interlacing:
            video_filters.append('idet')

        if crop:
            video_filters.append('cropdetect=0.094:16:0')

        if video_filters:
            video_filters = ','.join(video_filters)
            opts.extend(['-vf', video_filters])
        else:
            opts.append('-vn')

        if start:
            start = parse_time(start)
            opts.extend(['-ss', start])

        if duration:
            duration = parse_time(duration)
            opts.extend(['-t', duration])
        elif end:
            end = parse_time(end)
            opts.extend(['-to', end])

        for data in self.convert(infile, '/dev/null',
                                 opts, timeout, nice=nice, get_output=True):
            if isinstance(data, float):
                yield data
            else:
                interlace = None
                adjustement = None
                crop_size = None

                if audio_level:
                    match = re.search('Integrated loudness:\s+I:\s+(-?\d+\.\d)\s+LUFS(?s).+True peak:\s+Peak:\s+(-?\d+\.\d)\s+dBFS', data, re.UNICODE)
                    if match is None:
                        raise FFMpegConvertError(
                            'No audio analysis data.',
                            opts,
                            data
                        )
                    # Adjust audio volume so loudness will be close as possible
                    # as the target but max peak will not be above AUDIO_PEAK_MAX.
                    loudness = float(match.group(1))
                    peak = float(match.group(2))
                    # Check if audio is only noise.
                    # Values are good only for 16 bits audio.
                    if loudness < -64 and peak < -40:
                        adjustement = 'noise'
                    else:
                        loudness_adj = self.AUDIO_LOUDNESS_TARGET - loudness
                        if loudness_adj > 0:
                            peak_adj = self.AUDIO_PEAK_MAX - peak
                            if peak_adj < 0:
                                peak_adj = 0
                            adjustement = min(peak_adj, loudness_adj)
                        else:
                            adjustement = loudness_adj
                        # Don't adjust if adjustment is too small
                        if -1 < adjustement < 1:
                            adjustement = 0

                if interlacing:
                    match = re.search('Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)', data, re.UNICODE)
                    if match is None:
                        raise FFMpegConvertError(
                            'No interlaced data.',
                            opts,
                            data
                        )
                    tff = int(match.group(1))
                    bff = int(match.group(2))
                    progressive = int(match.group(3))
                    undetermined = int(match.group(4))
                    interlaced = tff + bff
                    total = interlaced + progressive + undetermined
                    # If more then 10% of frames are detected as interlaced,
                    # assume video is at least partly interlaced frames.
                    if interlaced > total / 10:
                        interlace = True
                    else:
                        interlace = False

                if crop:
                    crop_line = ''
                    for line in reversed(data.split('\n')):
                        if 'cropdetect' in line:
                            crop_line = line
                            break
                    match = re.search('crop=(-?\d+:-?\d+:-?\d+:-?\d)', crop_line, re.UNICODE)
                    if match is None:
                        raise FFMpegConvertError(
                            'No crop data.',
                            opts,
                            data
                        )
                    # If there's a negative value in cropdetect return value,
                    # don't crop, so return None
                    if not '-' in match.group(1):
                        crop_size = match.group(1)

                yield adjustement, interlace, crop_size

    def thumbnails_by_interval(self, source, output_pattern, interval=1,
                               max_width=None, max_height=None, autorotate=False,
                               sizing_policy=None, skip=False):
        """
        Create one or more thumbnails of video by a specified interval.
        """
        info = self.probe(source)
        if 'video' not in info:
            raise ValueError("Video stream not found.")

        video = info['video']

        src_width = video['video_width']
        src_height = video['video_height']
        w, h, filters = self._aspect_corrections(src_width, src_height,
                                                 max_width, max_height,
                                                 sizing_policy)
        w = self._div_by_2(w)
        h = self._div_by_2(h)

        if autorotate and 'rotate' in video['tags']:
            rotate_filter = {
                90: "transpose=1",
                180: "transpose=2,transpose=2",
                270: "transpose=2"
            }
            raw_rotate = video['tags'].get('rotate')
            if raw_rotate and int(raw_rotate) in rotate_filter.keys():
                src_rotate = int(raw_rotate)
                # apply filter
                filters = '{0}{2}{1}'.format(filters or '',
                                             rotate_filter.get(src_rotate) or '',
                                             ',' if filters and rotate_filter.get(src_rotate) else '')
                # swap height and width if vertical rotation
                if src_rotate in [90, 270]:
                    old_w = w
                    old_h = h
                    w = old_h
                    h = old_w

        if not os.path.exists(source) and not self.is_url(source):
            raise IOError('No such file: ' + source)

        cmds = [self.ffmpeg_path, ]

        if skip:
            if info.format.duration > 10:
                cmds.extend(['-ss', '10'])
            else:
                cmds.extend(['-ss', str(int(info.format.duration / 2))])

        cmds.extend(['-i', source, '-y', '-an'])
        cmds.extend(['-f', 'image2'])
        cmds.extend(['-s', "{0}x{1}".format(w, h)])
        cmds.extend(['-q:v', str(FFMpeg.DEFAULT_JPEG_QUALITY)])

        if filters:
            cmds.extend(['-vf', 'fps=fps=1/{0},{1}'.format(interval, filters)])
        else:
            cmds.extend(['-vf', 'fps=fps=1/{0}'.format(interval)])

        cmds.extend([output_pattern.format(count="%05d")])

        p = self._spawn(cmds)
        _, stderr_data = p.communicate()
        if stderr_data == '':
            raise FFMpegError('Error while calling ffmpeg binary')
        stderr_data.decode(console_encoding, "ignore")

    def thumbnail(self, fname, time, outfile, size=None, quality=DEFAULT_JPEG_QUALITY, crop=None, deinterlace=None):
        """
        Create a thumbnail of media file, and store it to outfile
        @param time: time point in seconds (float or int) or in HH:MM:SS format.
        @param size: Size, if specified, is WxH of the desired thumbnail.
            If not specified, the video resolution is used.
        @param quality: quality of jpeg file in range 2(best)-31(worst)
            recommended range: 2-6
        @param crop: crop size for all images specified like in FFMpeg.
        @param deinterlace: True to apply deinterlacing on thumbnail if needed.

        >>> FFMpeg().thumbnail('test1.ogg', 5, '/tmp/shot.png', '320x240')
        """
        if not os.path.exists(fname) and not self.is_url(fname):
            raise IOError('No such file: ' + fname)

        cmds = [self.ffmpeg_path, '-ss', parse_time(time), '-i', fname, '-y', '-an',
                '-f', 'image2', '-vframes', '1']
        if crop or deinterlace:
            cmds.append('-vf')
            filters = []
            if deinterlace:
                filters.append('idet,yadif=0:deint=interlaced')
            if crop:
                filters.append('crop={0}'.format(crop))
            cmds.append(','.join(filters))
        if size:
            cmds.extend(['-s', str(size)])

        cmds.extend([
            '-q:v', str(FFMpeg.DEFAULT_JPEG_QUALITY),
            outfile
        ])

        p = self._spawn(cmds)
        _, stderr_data = p.communicate()
        if stderr_data == '':
            raise FFMpegError('Error while calling ffmpeg binary')
        stderr_data.decode(console_encoding, "ignore")
        if not os.path.exists(outfile):
            raise FFMpegError('Error creating thumbnail: %s' % stderr_data)

    def _div_by_2(self, d):
        return d+1 if d % 2 else d

    def _aspect_corrections(self, sw, sh, max_width, max_height, sizing_policy):
        if not sw or not sh:
            return sw, sh, None

        # Original aspect ratio
        aspect = (1.0 * sw) / (1.0 * sh)

        # If we have only one dimension, we can easily calculate
        # the other to match the source aspect ratio
        if not max_width and not max_height:
            return max_width, max_height, None
        elif max_width and not max_height:
            max_height = int((1.0 * max_width) / aspect)
        elif max_height and not max_width:
            max_width = int(aspect * max_height)

        if sizing_policy not in ['Fit', 'Fill', 'Stretch', 'Keep', 'ShrinkToFit', 'ShrinkToFill']:
            print "invalid option {0}".format(sizing_policy)
            return sw, sh, None

        """
        Fit: FFMPEG scales the output video so it matches the value
        that you specified in either Max Width or Max Height without exceeding the other value."
        """
        if sizing_policy == 'Fit':
            if float(sh / sw) == float(max_height):
                return max_width, max_height, None
            elif float(sh / sw) < float(max_height):  # scaling height
                factor = float(float(max_height) / float(sh))
                return int(max_width * factor), max_height, None
            else:
                factor = float(float(max_width) / float(sw))
                return max_width, int(sh * factor), None

        """
        Fill: FFMPEG scales the output video so it matches the value that you specified
        in either Max Width or Max Height and matches or exceeds the other value. Elastic Transcoder
        centers the output video and then crops it in the dimension (if any) that exceeds the maximum value.
        """
        if sizing_policy == 'Fill':
            if float(sh / sw) == float(max_height):
                return max_width, max_height, None
            elif float(sh / sw) < float(max_height):  # scaling width
                factor = float(float(max_width) / float(sw))
                h0 = int(sh * factor)
                dh = (h0 - max_height) / 2
                return max_width, h0, 'crop={0}:{1}:{2}:0'.format(max_width, max_height, dh)
            else:
                factor = float(float(max_height) / float(sh))
                w0 = int(sw * factor)
                dw = (w0 - max_width) / 2
                return w0, max_height, 'crop={0}:{1}:{2}:0'.format(max_width, max_height, dw)

        """
        Stretch: FFMPEG stretches the output video to match the values that you specified for Max
        Width and Max Height. If the relative proportions of the input video and the output video are different,
        the output video will be distorted.
        """
        if sizing_policy == 'Stretch':
            return max_width, max_height, None

        """
        Keep: FFMPEG does not scale the output video. If either dimension of the input video exceeds
        the values that you specified for Max Width and Max Height, Elastic Transcoder crops the output video.
        """
        if sizing_policy == 'Keep':
            return sw, sh, None

        """
        ShrinkToFit: FFMPEG scales the output video down so that its dimensions match the values that
        you specified for at least one of Max Width and Max Height without exceeding either value. If you specify
        this option, Elastic Transcoder does not scale the video up.
        """
        if sizing_policy == 'ShrinkToFit':
            if sh > max_height or sw > max_width:
                if float(sh / sw) == float(max_height):
                    return max_width, max_height, None
                elif float(sh / sw) < float(max_height):  # target is taller
                    factor = float(float(max_height) / float(sh))
                    return int(sw * factor), max_height, None
                else:
                    factor = float(float(max_width) / float(sw))
                    return max_width, int(sh * factor), None
            else:
                return sw, sh, None

        """
        ShrinkToFill: FFMPEG scales the output video down so that its dimensions match the values that
        you specified for at least one of Max Width and Max Height without dropping below either value. If you specify
        this option, Elastic Transcoder does not scale the video up.
        """
        if sizing_policy == 'ShrinkToFill':
            if sh < max_height or sw < max_width:
                if float(sh / sw) == float(max_height):
                    return max_width, max_height, None
                elif float(sh / sw) < float(max_height):  # scaling width
                    factor = float(float(max_width) / float(sw))
                    h0 = int(sh * factor)
                    dh = (h0 - max_height) / 2
                    return max_width, h0, 'crop=%d:%d:%d:0' % (max_width, max_height, dh)
                else:
                    factor = float(float(max_height) / float(sh))
                    w0 = int(sw * factor)
                    dw = (w0 - max_width) / 2
                    return w0, max_height, 'crop={0}:{1}:{2}:0'.format(max_width, max_height, dw)
            else:
                return int(sw*factor), max_height, None

        assert False, sizing_policy

    def thumbnails(self, fname, option_list, crop=None, deinterlace=None):
        """
        Create one or more thumbnails of video.
        @param option_list: a list of tuples like:
            (time, outfile, size=None, quality=DEFAULT_JPEG_QUALITY)
            see documentation of `converter.FFMpeg.thumbnail()` for details.

        >>> FFMpeg().thumbnails('test1.ogg', [(5, '/tmp/shot.png', '320x240'),
        >>>                                   (10, '/tmp/shot2.png', None, 5)])
        """
        if not os.path.exists(fname) and not self.is_url(fname):
            raise IOError('No such file: ' + fname)

        errors = {}

        for options in option_list:
            time = options[0]
            outfile = options[1]
            size = options[2] if len(options) > 2 else None
            quality = options[3] if len(options) > 3 else FFMpeg.DEFAULT_JPEG_QUALITY

            try:
                self.thumbnail(fname, time, outfile, size, quality, crop, deinterlace)
            except Exception, err:
                errors[outfile] = err

        if errors:
            messages = u'; '.join(
                u'{0} gives error: {1}'.format(outfile, error)
                for outfile, error in errors.iteritems()
            )
            raise FFMpegError(messages)


def timecode_to_seconds(timecode):
    """
    Convert a valid timecode representation (a str) in seconds (as float).
    """
    try:
        return float(timecode)
    except ValueError:
        hours, minutes, seconds = timecode.split(':', 2)
        return (((int(hours) * 60) + int(minutes)) * 60) + float(seconds)


def seconds_to_timecode(seconds):
    """
    Convert seconds in int or float to timecode representation.
    """
    milliseconds = int(round((seconds % 1) * 1000))
    seconds = int(seconds)
    minutes = seconds / 60
    seconds = seconds % 60
    hours = minutes / 60
    minutes = minutes % 60
    return '{0:02d}:{1:02d}:{2:02d}.{3:03d}'.format(
        hours, minutes, seconds, milliseconds
    )


def parse_time(time):
    try:
        return str(float(time))
    except ValueError:
        match = re.search('([0-2]?\d):([0-5]?\d):([0-5]?\d)(?:\.(\d{3}))?$', time)
        if match:
            return '{0:02d}:{1:02d}:{2:02d}.{3:03d}'.format(*(int(i) if i else 0 for i in match.groups()))

    raise ValueError("Invalid 'time'. Should be in seconds or HH:MM:SS.xxx format.")
