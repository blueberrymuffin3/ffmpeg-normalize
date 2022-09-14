import os
import re
import json
import math

from ._errors import FFmpegNormalizeError
from ._cmd_utils import NUL, CommandRunner, dict_to_filter_opts
from ._logger import setup_custom_logger

logger = setup_custom_logger("ffmpeg_normalize")


class MediaStream(object):
    def __init__(self, ffmpeg_normalize, media_file, stream_type, stream_id):
        """
        Arguments:
            media_file {MediaFile} -- parent media file
            stream_type {str} -- stream type
            stream_id {int} -- Audio stream id
        """
        self.ffmpeg_normalize = ffmpeg_normalize
        self.media_file = media_file
        self.stream_type = stream_type
        self.stream_id = stream_id

    def __repr__(self):
        return "<{}, {} stream {}>".format(
            os.path.basename(self.media_file.input_file),
            self.stream_type,
            self.stream_id,
        )


class VideoStream(MediaStream):
    def __init__(self, ffmpeg_normalize, media_file, stream_id):
        super(VideoStream, self).__init__(
            media_file, ffmpeg_normalize, "video", stream_id
        )


class SubtitleStream(MediaStream):
    def __init__(self, ffmpeg_normalize, media_file, stream_id):
        super(SubtitleStream, self).__init__(
            media_file, ffmpeg_normalize, "subtitle", stream_id
        )


class AudioStream(MediaStream):
    def __init__(
        self,
        ffmpeg_normalize,
        media_file,
        stream_id,
        sample_rate=None,
        bit_depth=None,
        duration=None,
    ):
        """
        Arguments:
            sample_rate {int} -- in Hz
            bit_depth {int}
            duration {int} -- duration in seconds
        """
        super(AudioStream, self).__init__(
            media_file, ffmpeg_normalize, "audio", stream_id
        )

        self.loudness_statistics = {"ebu": None, "mean": None, "max": None}

        self.sample_rate = sample_rate
        self.bit_depth = bit_depth

        self.duration = duration

        if (
            self.ffmpeg_normalize.normalization_type == "ebu"
            and self.duration
            and self.duration <= 3
        ):
            logger.warn(
                "Audio stream has a duration of less than 3 seconds. "
                "Normalization may not work. "
                "See https://github.com/slhck/ffmpeg-normalize/issues/87 for more info."
            )

    def __repr__(self):
        return "<{}, audio stream {}>".format(
            os.path.basename(self.media_file.input_file), self.stream_id
        )

    @staticmethod
    def _constrain(number, min_range, max_range, name=None):
        """
        Constrain a number between two values
        """
        if min_range > max_range:
            raise ValueError("min must be smaller than max")
        result = max(min(number, max_range), min_range)
        if result != number and name is not None:
            logger.warning(
                f"Constraining {name} to range of [{min_range}, {max_range}]: {number} -> {result}"
            )
        return result

    def get_stats(self):
        """
        Return statistics
        """
        stats = {
            "input_file": self.media_file.input_file,
            "output_file": self.media_file.output_file,
            "stream_id": self.stream_id,
        }
        stats.update(self.loudness_statistics)
        return stats

    def get_pcm_codec(self):
        if not self.bit_depth:
            return "pcm_s16le"
        elif self.bit_depth <= 8:
            return "pcm_s8"
        elif self.bit_depth in [16, 24, 32, 64]:
            return f"pcm_s{self.bit_depth}le"
        else:
            logger.warning(
                f"Unsupported bit depth {self.bit_depth}, falling back to pcm_s16le"
            )
            return "pcm_s16le"

    def _get_filter_str_with_pre_filter(self, current_filter):
        """
        Get a filter string for current_filter, with the pre-filter
        added before. Applies the input label before.
        """
        input_label = f"[0:{self.stream_id}]"
        filter_chain = []
        if self.media_file.ffmpeg_normalize.pre_filter:
            filter_chain.append(self.media_file.ffmpeg_normalize.pre_filter)
        filter_chain.append(current_filter)
        filter_str = input_label + ",".join(filter_chain)
        return filter_str

    def parse_astats(self):
        """
        Use ffmpeg with astats filter to get the mean (RMS) and max (peak) volume of the input file.
        """
        logger.info(f"Running first pass astats filter for stream {self.stream_id}")

        filter_str = self._get_filter_str_with_pre_filter(
            "astats=measure_overall=Peak_level+RMS_level:measure_perchannel=0"
        )

        cmd = [
            self.media_file.ffmpeg_normalize.ffmpeg_exe,
            "-nostdin",
            "-y",
            "-i",
            self.media_file.input_file,
            "-filter_complex",
            filter_str,
            "-vn",
            "-sn",
            "-f",
            "null",
            NUL,
        ]

        cmd_runner = CommandRunner(cmd)
        for progress in cmd_runner.run_ffmpeg_command():
            yield progress
        output = cmd_runner.get_output()

        logger.debug(
            f"astats command output: {CommandRunner.prune_ffmpeg_progress_from_output(output)}"
        )

        mean_volume_matches = re.findall(r"RMS level dB: ([\-\d\.]+)", output)
        if mean_volume_matches:
            if mean_volume_matches[0] == "-":
                self.loudness_statistics["mean"] = -math.inf
            else:
                self.loudness_statistics["mean"] = float(mean_volume_matches[0])
        else:
            raise FFmpegNormalizeError(
                f"Could not get mean volume for {self.media_file.input_file}"
            )

        max_volume_matches = re.findall(r"Peak level dB: ([\-\d\.]+)", output)
        if max_volume_matches:
            if max_volume_matches[0] == "-":
                self.loudness_statistics["max"] = -math.inf
            else:
                self.loudness_statistics["max"] = float(max_volume_matches[0])
        else:
            raise FFmpegNormalizeError(
                f"Could not get max volume for {self.media_file.input_file}"
            )

    def parse_loudnorm_stats(self):
        """
        Run a first pass loudnorm filter to get measured data.
        """
        logger.info(f"Running first pass loudnorm filter for stream {self.stream_id}")

        opts = {
            "i": self.media_file.ffmpeg_normalize.target_level,
            "lra": self.media_file.ffmpeg_normalize.loudness_range_target,
            "tp": self.media_file.ffmpeg_normalize.true_peak,
            "offset": self.media_file.ffmpeg_normalize.offset,
            "print_format": "json",
        }

        if self.media_file.ffmpeg_normalize.dual_mono:
            opts["dual_mono"] = "true"

        filter_str = self._get_filter_str_with_pre_filter(
            "loudnorm=" + dict_to_filter_opts(opts)
        )

        cmd = [
            self.media_file.ffmpeg_normalize.ffmpeg_exe,
            "-nostdin",
            "-y",
            "-i",
            self.media_file.input_file,
            "-filter_complex",
            filter_str,
            "-vn",
            "-sn",
            "-f",
            "null",
            NUL,
        ]

        cmd_runner = CommandRunner(cmd)
        for progress in cmd_runner.run_ffmpeg_command():
            yield progress
        output = cmd_runner.get_output()

        logger.debug(
            f"Loudnorm first pass command output: {CommandRunner.prune_ffmpeg_progress_from_output(output)}"
        )

        output_lines = [line.strip() for line in output.split("\n")]

        self.loudness_statistics["ebu"] = AudioStream._parse_loudnorm_output(
            output_lines
        )

    @staticmethod
    def _parse_loudnorm_output(output_lines):
        loudnorm_start = False
        loudnorm_end = False
        for index, line in enumerate(output_lines):
            if line.startswith("[Parsed_loudnorm"):
                loudnorm_start = index + 1
                continue
            if loudnorm_start and line.startswith("}"):
                loudnorm_end = index + 1
                break

        if not (loudnorm_start and loudnorm_end):
            raise FFmpegNormalizeError(
                "Could not parse loudnorm stats; no loudnorm-related output found"
            )

        try:
            loudnorm_stats = json.loads(
                "\n".join(output_lines[loudnorm_start:loudnorm_end])
            )

            logger.debug(f"Loudnorm stats parsed: {json.dumps(loudnorm_stats)}")

            for key in [
                "input_i",
                "input_tp",
                "input_lra",
                "input_thresh",
                "output_i",
                "output_tp",
                "output_lra",
                "output_thresh",
                "target_offset",
            ]:
                # handle infinite values
                if float(loudnorm_stats[key]) == -float("inf"):
                    loudnorm_stats[key] = -99
                elif float(loudnorm_stats[key]) == float("inf"):
                    loudnorm_stats[key] = 0
                else:
                    # convert to floats
                    loudnorm_stats[key] = float(loudnorm_stats[key])

            return loudnorm_stats
        except Exception as e:
            raise FFmpegNormalizeError(
                f"Could not parse loudnorm stats; wrong JSON format in string: {e}"
            )

    def get_second_pass_opts_ebu(self):
        """
        Return second pass loudnorm filter options string for ffmpeg
        """

        if not self.loudness_statistics["ebu"]:
            raise FFmpegNormalizeError(
                "First pass not run, you must call parse_loudnorm_stats first"
            )

        if float(self.loudness_statistics["ebu"]["input_i"]) > 0:
            logger.warn(
                f"Input file had measured input loudness greater than zero ({self.loudness_statistics['ebu']['input_i']}), capping at 0"
            )
            self.loudness_statistics["ebu"]["input_i"] = 0

        will_use_dynamic_mode = self.media_file.ffmpeg_normalize.dynamic

        if self.media_file.ffmpeg_normalize.keep_loudness_range_target:
            logger.debug("Keeping target loudness range in second pass loudnorm filter")
            self.media_file.ffmpeg_normalize.loudness_range_target = (
                self.loudness_statistics["ebu"]["input_lra"]
            )

        if (
            self.media_file.ffmpeg_normalize.loudness_range_target
            < self.loudness_statistics["ebu"]["input_lra"]
            and not will_use_dynamic_mode
        ):
            logger.warn(
                f"Input file had loudness range of {self.loudness_statistics['ebu']['input_lra']}, which is larger than the loudness range target ({self.media_file.ffmpeg_normalize.loudness_range_target}). "
                "Normalization will revert to dynamic mode. Choose a higher target loudness range if you want linear normalization. "
                "Alternatively, use the --keep-loudness-range-target option to keep the target loudness range from the input."
            )
            will_use_dynamic_mode = True

        if will_use_dynamic_mode and not self.ffmpeg_normalize.sample_rate:
            logger.warn(
                "In dynamic mode, the sample rate will automatically be set to 192 kHz by the loudnorm filter. "
                "Specify -ar/--sample-rate to override it."
            )

        stats = self.loudness_statistics["ebu"]

        opts = {
            "i": self.media_file.ffmpeg_normalize.target_level,
            "lra": self.media_file.ffmpeg_normalize.loudness_range_target,
            "tp": self.media_file.ffmpeg_normalize.true_peak,
            "offset": self._constrain(float(stats["target_offset"]), -99, 99, name="target_offset"),
            "measured_i": self._constrain(float(stats["input_i"]), -99, 0, name="input_i"),
            "measured_lra": self._constrain(float(stats["input_lra"]), 0, 99, name="input_lra"),
            "measured_tp": self._constrain(float(stats["input_tp"]), -99, 99, name="input_tp"),
            "measured_thresh": self._constrain(float(stats["input_thresh"]), -99, 0, name="input_thresh"),
            "linear": "false" if self.media_file.ffmpeg_normalize.dynamic else "true",
            "print_format": "json",
        }

        if self.media_file.ffmpeg_normalize.dual_mono:
            opts["dual_mono"] = "true"

        return "loudnorm=" + dict_to_filter_opts(opts)

    def get_second_pass_opts_peakrms(self):
        """
        Set the adjustment gain based on chosen option and mean/max volume,
        return the matching ffmpeg volume filter.
        """
        normalization_type = self.media_file.ffmpeg_normalize.normalization_type
        target_level = self.media_file.ffmpeg_normalize.target_level

        if normalization_type == "peak":
            adjustment = 0 + target_level - self.loudness_statistics["max"]
        elif normalization_type == "rms":
            adjustment = target_level - self.loudness_statistics["mean"]
        else:
            raise FFmpegNormalizeError(
                "Can only set adjustment for peak and RMS normalization"
            )

        logger.info(
            "Adjusting stream {} by {} dB to reach {}".format(
                self.stream_id, adjustment, target_level
            )
        )

        if self.loudness_statistics["max"] + adjustment > 0:
            logger.warning(
                "Adjusting will lead to clipping of {} dB".format(
                    self.loudness_statistics["max"] + adjustment
                )
            )

        return f"volume={adjustment}dB"
