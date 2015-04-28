#!/usr/bin/python

import os

from converter.avcodecs import video_codec_list, audio_codec_list, subtitle_codec_list
from converter.formats import format_list
from converter.ffmpeg import FFMpeg, parse_time, FFMpegError


class ConverterError(Exception):
    pass


class Converter(object):
    """
    Converter class, encapsulates formats and codecs.

    >>> c = Converter()
    """

    def __init__(self, ffmpeg_path=None, ffprobe_path=None):
        """
        Initialize a new Converter object.
        """

        self.ffmpeg = FFMpeg(ffmpeg_path=ffmpeg_path,
                             ffprobe_path=ffprobe_path)
        self.video_codecs = {}
        self.audio_codecs = {}
        self.subtitle_codecs = {}
        self.formats = {}

        for cls in audio_codec_list:
            name = cls.codec_name
            self.audio_codecs[name] = cls

        for cls in video_codec_list:
            name = cls.codec_name
            self.video_codecs[name] = cls

        for cls in subtitle_codec_list:
            name = cls.codec_name
            self.subtitle_codecs[name] = cls

        for cls in format_list:
            name = cls.format_name
            self.formats[name] = cls

    def parse_options(self, opt, twopass=None):
        """
        Parse format/codec options and prepare raw ffmpeg option list.
        """
        if not isinstance(opt, dict):
            raise ConverterError('Invalid output specification')

        if 'format' not in opt:
            raise ConverterError('Format not specified')

        f = opt['format']
        if f not in self.formats:
            raise ConverterError('Requested unknown format: ' + str(f))

        format_options = self.formats[f]().parse_options(opt)
        if format_options is None:
            raise ConverterError('Unknown container format error')

        if 'audio' not in opt and 'video' not in opt:
            raise ConverterError('Neither audio nor video streams requested')

        # audio options
        if 'audio' not in opt or twopass == 1:
            opt_audio = {'codec': None}
        else:
            opt_audio = opt['audio']
            if not isinstance(opt_audio, dict) or 'codec' not in opt_audio:
                raise ConverterError('Invalid audio codec specification')

        c = opt_audio['codec']
        if c not in self.audio_codecs:
            raise ConverterError('Requested unknown audio codec ' + str(c))

        audio_options = self.audio_codecs[c]().parse_options(opt_audio)
        if audio_options is None:
            raise ConverterError('Unknown audio codec error')

        # video options
        if 'video' not in opt:
            opt_video = {'codec': None}
        else:
            opt_video = opt['video']
            if not isinstance(opt_video, dict) or 'codec' not in opt_video:
                raise ConverterError('Invalid video codec specification')

        c = opt_video['codec']
        if c not in self.video_codecs:
            raise ConverterError('Requested unknown video codec ' + str(c))

        video_options = self.video_codecs[c]().parse_options(opt_video)
        if video_options is None:
            raise ConverterError('Unknown video codec error')

        if 'subtitle' not in opt:
            opt_subtitle = {'codec': None}
        else:
            opt_subtitle = opt['subtitle']
            if not isinstance(opt_subtitle, dict) or 'codec' not in opt_subtitle:
                raise ConverterError('Invalid subtitle codec specification')

        c = opt_subtitle['codec']
        if c not in self.subtitle_codecs:
            raise ConverterError('Requested unknown subtitle codec ' + str(c))

        subtitle_options = self.subtitle_codecs[c]().parse_options(opt_subtitle)
        if subtitle_options is None:
            raise ConverterError('Unknown subtitle codec error')

        if 'map' in opt:
            m = opt['map']
            if not type(m) == int:
                raise ConverterError('map needs to be int')
            else:
                format_options.extend(['-map', str(m)])

        if 'start' in opt:
            start = parse_time(opt['start'])
            format_options.extend(['-ss', start])

        if 'duration' in opt:
            duration = parse_time(opt['duration'])
            format_options.extend(['-t', duration])

        # aggregate all options
        optlist = audio_options + video_options + subtitle_options + format_options

        if twopass == 1:
            optlist.extend(['-pass', '1'])
        elif twopass == 2:
            optlist.extend(['-pass', '2'])

        return optlist

    def convert(self, infile, outfile, options, twopass=False, timeout=10, nice=None):
        """
        Convert media file (infile) according to specified options, and
        save it to outfile. For two-pass encoding, specify the pass (1 or 2)
        in the twopass parameter.

        Options should be passed as a dictionary. The keys are:
            * format (mandatory, string) - container format; see
              formats.BaseFormat for list of supported formats
            * audio (optional, dict) - audio codec and options; see
              avcodecs.AudioCodec for list of supported options
            * video (optional, dict) - video codec and options; see
              avcodecs.VideoCodec for list of supported options
            * map (optional, int) - can be used to map all content of stream 0

        Multiple audio/video streams are not supported. The output has to
        have at least an audio or a video stream (or both).

        Convert returns a generator that needs to be iterated to drive the
        conversion process. The generator will periodically yield timecode
        of currently processed part of the file (ie. at which second in the
        content is the conversion process currently).

        The optional timeout argument specifies how long should the operation
        be blocked in case ffmpeg gets stuck and doesn't report back. This
        doesn't limit the total conversion time, just the amount of time
        Converter will wait for each update from ffmpeg. As it's usually
        less than a second, the default of 10 is a reasonable default. To
        disable the timeout, set it to None. You may need to do this if
        using Converter in a threading environment, since the way the
        timeout is handled (using signals) has special restriction when
        using threads.

        >>> conv = Converter().convert('test1.ogg', '/tmp/output.mkv', {
        ...    'format': 'mkv',
        ...    'audio': { 'codec': 'aac' },
        ...    'video': { 'codec': 'h264' }
        ... })

        >>> for timecode in conv:
        ...   pass # can be used to inform the user about the progress
        """

        if not isinstance(options, dict):
            raise ConverterError('Invalid options')

        if not os.path.exists(infile) and not self.ffmpeg.is_url(infile):
            raise ConverterError("Source file doesn't exist: " + infile)

        info = self.ffmpeg.probe(infile)
        if info is None:
            raise ConverterError("Can't get information about source file")

        if 'video' not in info and 'audio' not in info:
            raise ConverterError('Source file has no audio or video streams')

        if 'video' in info and 'video' in options:
            options = options.copy()
            v = options['video'] = options['video'].copy()
            v['src_width'] = info['video']['width']
            v['src_height'] = info['video']['height']
            if 'tags' in info['video'] and 'rotate' in info['video']['tags']:
                v['src_rotate'] = info['video']['tags']['rotate']

        if info['format']['duration'] < 0.01:
            raise ConverterError('Zero-length media')

        if twopass:
            optlist1 = self.parse_options(options, 1)
            for timecode in self.ffmpeg.convert(infile, outfile, optlist1,
                                                timeout=timeout, nice=nice):
                yield int((50.0 * timecode) / info['format']['duration'])

            optlist2 = self.parse_options(options, 2)
            for timecode in self.ffmpeg.convert(infile, outfile, optlist2,
                                                timeout=timeout, nice=nice):
                yield int(50.0 + (50.0 * timecode) / info['format']['duration'])
        else:
            optlist = self.parse_options(options, twopass)
            for timecode in self.ffmpeg.convert(infile, outfile, optlist,
                                                timeout=timeout, nice=nice):
                yield int((100.0 * timecode) / info['format']['duration'])

    def analyze(self, infile, audio_level=True, interlacing=True, crop=False, start=None, duration=None, timeout=10, nice=None):
        """
        Analyze the video frames to find if the video need to be deinterlaced.
        Or/and analyze the audio to find if the audio need to be normalize
        and by how much. Both analyses are together so FFMpeg can do both
        analyses in the same pass.

        :param audio_level: Set it to True to get by how much dB the audio need
        to be normalize, defaults to True.
        :param interlacing: Set it to True to check if the video need to be
        deinterlaced, defaults to True.
        :param timeout: How long should the operation be blocked in case ffmpeg
        gets stuck and doesn't report back, defaults to 10 sec.
        """
        if not os.path.exists(infile) and not self.ffmpeg.is_url(infile):
            raise ConverterError("Source file doesn't exist: " + infile)

        info = self.ffmpeg.probe(infile)
        if info is None:
            raise ConverterError("Can't get information about source file")

        if 'video' not in info and 'audio' not in info:
            raise ConverterError('Source file has no audio or video streams')

        if 'audio' not in info:
            audio_level = False

        if 'video' not in info:
            interlacing = False
            crop = False

        if info['format']['duration'] < 0.01:
            raise ConverterError('Zero-length media')
        for timecode in self.ffmpeg.analyze(infile, audio_level, interlacing,
                                            crop, start, duration, timeout, nice):
            if isinstance(timecode, float):
                yield int((100.0 * timecode) / info['format']['duration'])
            else:
                yield timecode

    def probe(self, *args, **kwargs):
        """
        Examine the media file. See the documentation of
        converter.FFMpeg.probe() for details.

        :param posters_as_video: Take poster images (mainly for audio files) as
            A video stream, defaults to True
        """
        return self.ffmpeg.probe(*args, **kwargs)

    def validate(self, source):
        if not os.path.exists(source) and not self.ffmpeg.is_url(source):
            yield "Source file doesn't exist: " + source

        info = self.ffmpeg.probe(source)
        if info is None:
            yield 'no info'

        if 'video' not in info and 'audio' not in info:
            yield 'no stream'

        processed = self.ffmpeg.convert(source, '/dev/null', ['-f', 'rawvideo'],
                                        timeout=100, nice=15, get_output=True)
        for timecode in processed:
            if isinstance(timecode, basestring):
                if 'rror while decoding' in timecode:
                    yield 'error'
                yield None
            else:
                yield timecode

    def thumbnail(self, *args, **kwargs):
        """
        Create a thumbnail of the media file. See the documentation of
        converter.FFMpeg.thumbnail() for details.
        """
        return self.ffmpeg.thumbnail(*args, **kwargs)

    def thumbnails(self, *args, **kwargs):
        """
        Create one or more thumbnail of the media file. See the documentation
        of converter.FFMpeg.thumbnails() for details.
        """
        return self.ffmpeg.thumbnails(*args, **kwargs)

    def thumbnails_by_interval(self, *args, **kwargs):
        """
        Create one or more thumbnail of the media file. See the documentation
        of converter.FFMpeg.thumbnails() for details.
        """
        return self.ffmpeg.thumbnails_by_interval(*args, **kwargs)


def is_faststart(source):
    """
    Check if the given file is 'faststart' or not.
    """
    with open(source) as source:
        head = source.read(64)
    if 'moov' in head:
        return True
    return False
