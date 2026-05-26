import json

from pyflows.config import load_config
from pyflows.plan import plan_from_probe
from pyflows.probe import parse_probe_output


SAMPLE_PROBE = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "tags": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "eng", "title": ""}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng", "title": ""}},
    ]
}


def test_plan_from_probe_detects_required_changes(tmp_config) -> None:
    config = load_config(tmp_config)
    plan = plan_from_probe(
        "/media/test.mp4",
        parse_probe_output(json.dumps(SAMPLE_PROBE)),
        config.profiles["test"],
    )

    assert plan.compliant is False
    assert plan.should_skip is False
    assert plan.video.action == "encode"
    assert plan.output.target_container == "mkv"
    assert any(reason.scope == "container" for reason in plan.reasons)
    assert any(reason.scope == "audio" for reason in plan.reasons)
    assert any(reason.scope == "subtitles" for reason in plan.reasons)
