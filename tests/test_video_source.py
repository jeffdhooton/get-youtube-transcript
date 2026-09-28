from pathlib import Path

import pytest

from sources import AccessDenied, CaptureError, RequestFailed, RequestStopped, Sources


def video_format(height, *, codec="avc1", protocol="https", audio="none", drm=False):
    return {"format_id": str(height), "ext": "mp4", "height": height,
            "width": height * 16 // 9, "vcodec": codec, "acodec": audio,
            "protocol": protocol, "url": "https://example.test/video", "has_drm": drm}


def test_video_selection_uses_bounded_direct_format():
    from sources import choose_video
    formats = [video_format(2160), video_format(1080), video_format(720),
               video_format(1080, drm=True), video_format(1080, protocol="m3u8_native")]
    assert choose_video({"formats": formats}) == formats[1]
    with pytest.raises(CaptureError):
        choose_video({"formats": [video_format(0, codec="none")]})


@pytest.mark.parametrize("failure,expected", [(RequestStopped("403"), AccessDenied),
                                              (RequestFailed("connection lost"), CaptureError)])
def test_video_download_propagates_refusal_or_network_failure(tmp_path, monkeypatch, failure, expected):
    source = Sources()
    source.info = {"id": "abcdefghijk", "formats": [video_format(720)]}
    def fail(info):
        raise failure
    monkeypatch.setattr(source.ydl, "process_info", fail)
    try:
        with pytest.raises(expected):
            source.download_video(tmp_path)
        assert not list(tmp_path.iterdir())
    finally:
        source.close()


@pytest.mark.parametrize("complete", [True, False])
def test_video_download_requires_completed_media(tmp_path, monkeypatch, complete):
    source = Sources()
    source.info = {"id": "abcdefghijk", "formats": [video_format(720)]}
    def download(info):
        assert info["format_id"] == "720"
        Path(tmp_path / ("video.mp4" if complete else "video.mp4.part")).write_bytes(b"fixture")
    monkeypatch.setattr(source.ydl, "process_info", download)
    try:
        if complete:
            assert source.download_video(tmp_path) == tmp_path / "video.mp4"
        else:
            with pytest.raises(CaptureError, match="complete"):
                source.download_video(tmp_path)
    finally:
        source.close()
