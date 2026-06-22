import re

from .media_profile import MediaProfile


class FormatDecisionEngine:
    """
    Decides the best stream format and sorting options based on MediaProfile.
    Replaces legacy height-only format logic.
    """

    @classmethod
    def build_format_selector(cls, profile: MediaProfile) -> str:
        if profile.mode == "audio":
            return cls._build_audio_selector(profile)
        return cls._build_video_selector(profile)

    @classmethod
    def build_format_sort_spec(cls, profile: MediaProfile) -> str:
        if profile.mode == "audio":
            return "-abr,-asr"
        return "-hdr,-res,-fps,-size,-br"

    @classmethod
    def _build_audio_selector(cls, profile: MediaProfile) -> str:
        codec = profile.preferred_audio_codec
        candidates = []
        if codec == "mp4a":
            candidates.append("bestaudio[acodec^=mp4a]")
        elif codec == "opus":
            candidates.append("bestaudio[acodec^=opus]")
        elif codec == "vorbis":
            candidates.append("bestaudio[acodec^=vorbis]")
        elif codec == "mp3":
            candidates.append("bestaudio[ext=mp3]")

        candidates.append("bestaudio/best")
        return "/".join(candidates)

    @classmethod
    def _extract_height(cls, resolution: str) -> int:
        res = str(resolution).lower().replace("kbps", "")
        if res in {"2160p", "4k"}:
            return 2160
        if res in {"4320p", "8k"}:
            return 4320
        match = re.search(r"(\d+)", res)
        if match:
            return int(match.group(1))
        return 1080

    @classmethod
    def _build_video_selector(cls, profile: MediaProfile) -> str:
        h = cls._extract_height(profile.resolution)
        is_hdr = profile.dynamic_range in {"HDR", "DV"}
        codec = profile.preferred_video_codec
        if codec == "av01":
            codecs = ["av01", "hev1", "vp09.02", "vp09", "avc1"]
        elif codec == "vp9":
            codecs = ["vp09.02", "vp09", "av01", "hev1", "avc1"]
        elif codec == "hev1":
            codecs = ["hev1", "hvc1", "hevc", "h265", "av01", "vp09.02", "vp09", "avc1"]
        else:
            codecs = ["avc1", "h264", "vp09", "hev1", "av01"]

        if profile.force_hardware_decode_safe:
            codecs = [c for c in codecs if c in {"avc1", "h264"}]

        audio_codec = profile.preferred_audio_codec
        if audio_codec == "mp4a":
            audio_ext = "[acodec^=mp4a]"
        elif audio_codec == "opus":
            audio_ext = "[acodec^=opus]"
        elif audio_codec == "vorbis":
            audio_ext = "[acodec^=vorbis]"
        else:
            audio_ext = ""

        selectors = []
        seen = set()

        def add_selector(sel: str):
            if sel not in seen:
                selectors.append(sel)
                seen.add(sel)

        for c in codecs:
            base = f"bestvideo*[height<={h}][vcodec!=none]"
            if is_hdr:
                dynamic_range = "DV" if profile.dynamic_range == "DV" else "HDR"
                add_selector(f"{base}[fps>=50][vcodec^={c}][dynamic_range={dynamic_range}]+bestaudio{audio_ext}")
            add_selector(f"{base}[fps>=50][vcodec^={c}]+bestaudio{audio_ext}")

        for c in codecs:
            base = f"bestvideo*[height<={h}][vcodec!=none]"
            if is_hdr:
                dynamic_range = "DV" if profile.dynamic_range == "DV" else "HDR"
                add_selector(f"{base}[vcodec^={c}][dynamic_range={dynamic_range}]+bestaudio{audio_ext}")
            add_selector(f"{base}[vcodec^={c}]+bestaudio{audio_ext}")

        add_selector(f"bestvideo*[height<={h}][vcodec!=none]+bestaudio/best[height<={h}]")
        add_selector(f"bestvideo*[vcodec!=none]+bestaudio/best[height<={h}]")
        add_selector("best")
        return "/".join(selectors)
