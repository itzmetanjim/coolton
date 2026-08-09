import json

from listeners.views.byok_views import build_add_endpoint_modal, build_edit_endpoint_modal


def _inputs(modal):
    return {b["block_id"]: b for b in modal["blocks"] if b["type"] == "input"}


def test_add_endpoint_modal_structure():
    modal = build_add_endpoint_modal()
    assert modal["type"] == "modal"
    assert modal["callback_id"] == "byok_add_submit"
    assert modal["title"]["text"] == "Add Endpoint"
    assert modal["submit"]["text"] == "Add"

    inputs = _inputs(modal)
    assert set(inputs) == {"ep_name", "ep_base_url", "ep_api_key", "ep_model"}
    for block_id, block in inputs.items():
        assert block["element"]["type"] == "plain_text_input"
        assert block["element"]["action_id"] == "value"


def test_edit_endpoint_modal_prefills_values():
    ep = {
        "id": "ep_abc",
        "name": "My Endpoint",
        "base_url": "https://api.example.com/v1/",
        "model": "my-model",
    }
    modal = build_edit_endpoint_modal(ep)

    assert modal["callback_id"] == "byok_edit_submit"
    assert json.loads(modal["private_metadata"]) == {"ep_id": "ep_abc"}
    assert modal["title"]["text"] == "Edit Endpoint"

    inputs = _inputs(modal)
    assert inputs["ep_name"]["element"]["initial_value"] == "My Endpoint"
    assert inputs["ep_base_url"]["element"]["initial_value"] == "https://api.example.com/v1/"
    assert inputs["ep_model"]["element"]["initial_value"] == "my-model"
    # api key input is optional (leave blank to keep current)
    assert inputs["ep_api_key"]["optional"] is True
