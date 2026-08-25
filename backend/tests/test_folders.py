"""Conversation-folder tests — personal sidebar organization.

Proves: folder CRUD is scoped to its owner (strangers 404, no oracle); a chat
files into the owner's own folder only — not into another user's folder, and
not by a share recipient; deleting a folder unfiles its chats (never deletes
them); the conversations listing carries folder_id for the owner but never
leaks the owner's filing to a share recipient.

Run from the backend directory:
    python -m pytest tests/test_folders.py -q
"""
import os
import tempfile

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-folders-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi import HTTPException

from app import chat, db
from app.chat import FolderIn, MoveIn

ANA = {"id": "u-ana", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}
BOB = {"id": "u-bob", "email": "bob@studio.test", "role": "analyst", "name": "Bob"}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    yield


def test_folder_crud_and_owner_scoping():
    f = chat.create_folder(FolderIn(name="  Q3 revenue  "), user=ANA)
    assert f["name"] == "Q3 revenue"                      # trimmed
    assert f["id"] in {x["id"] for x in chat.folders(user=ANA)["folders"]}
    assert chat.folders(user=BOB)["folders"] == []        # not Bob's

    chat.rename_folder(f["id"], FolderIn(name="Q3"), user=ANA)
    assert any(x["name"] == "Q3" for x in chat.folders(user=ANA)["folders"])

    for op in (lambda: chat.rename_folder(f["id"], FolderIn(name="mine"), user=BOB),
               lambda: chat.delete_folder(f["id"], user=BOB)):
        with pytest.raises(HTTPException) as e:
            op()
        assert e.value.status_code == 404                 # stranger sees a 404, not a 403


def test_empty_folder_name_rejected():
    with pytest.raises(HTTPException) as e:
        chat.create_folder(FolderIn(name="   "), user=ANA)
    assert e.value.status_code == 400


def test_duplicate_folder_names_rejected_per_user():
    chat.create_folder(FolderIn(name="Reports"), user=ANA)
    for dup in ("Reports", "  reports  "):        # case-insensitive, trimmed
        with pytest.raises(HTTPException) as e:
            chat.create_folder(FolderIn(name=dup), user=ANA)
        assert e.value.status_code == 400
    # Another user may reuse the name; renaming into a collision is refused,
    # while renaming a folder to itself (case tweak) is allowed.
    other = chat.create_folder(FolderIn(name="Reports"), user=BOB)
    second = chat.create_folder(FolderIn(name="Drafts"), user=ANA)
    with pytest.raises(HTTPException) as e:
        chat.rename_folder(second["id"], FolderIn(name="reports"), user=ANA)
    assert e.value.status_code == 400
    assert chat.rename_folder(other["id"], FolderIn(name="REPORTS"), user=BOB)[
        "name"] == "REPORTS"


def test_move_files_and_unfiles_and_lists_folder_id():
    f = chat.create_folder(FolderIn(name="filed"), user=ANA)
    cid = db.create_conversation(ANA["id"], "revenue by region")

    chat.move_conversation(cid, MoveIn(folder_id=f["id"]), user=ANA)
    row = next(c for c in chat.conversations(user=ANA) if c["id"] == cid)
    assert row["folder_id"] == f["id"]

    chat.move_conversation(cid, MoveIn(folder_id=None), user=ANA)   # back to root
    row = next(c for c in chat.conversations(user=ANA) if c["id"] == cid)
    assert row["folder_id"] is None


def test_cannot_file_into_someone_elses_folder():
    theirs = chat.create_folder(FolderIn(name="bobs"), user=BOB)
    cid = db.create_conversation(ANA["id"], "mine")
    with pytest.raises(HTTPException) as e:
        chat.move_conversation(cid, MoveIn(folder_id=theirs["id"]), user=ANA)
    assert e.value.status_code == 404


def test_share_recipient_cannot_move_and_never_sees_owner_filing():
    f = chat.create_folder(FolderIn(name="private filing"), user=ANA)
    cid = db.create_conversation(ANA["id"], "shared chat")
    chat.move_conversation(cid, MoveIn(folder_id=f["id"]), user=ANA)
    db.share_conversation(cid, BOB["id"], "edit")

    # Bob sees the chat, but unfiled — Ana's folders are hers alone.
    row = next(c for c in chat.conversations(user=BOB) if c["id"] == cid)
    assert row["shared"] is True and row["folder_id"] is None

    bobs = chat.create_folder(FolderIn(name="bob side"), user=BOB)
    # Bob can SEE the shared chat, so this is a 403 (owner-only), not a 404 —
    # the no-oracle rule only applies to conversations invisible to the caller.
    with pytest.raises(HTTPException) as e:
        chat.move_conversation(cid, MoveIn(folder_id=bobs["id"]), user=BOB)
    assert e.value.status_code == 403
    # And Ana's filing is untouched by the attempt.
    row = next(c for c in chat.conversations(user=ANA) if c["id"] == cid)
    assert row["folder_id"] == f["id"]


def test_delete_folder_unfiles_but_keeps_chats():
    f = chat.create_folder(FolderIn(name="doomed"), user=ANA)
    cid = db.create_conversation(ANA["id"], "survivor")
    chat.move_conversation(cid, MoveIn(folder_id=f["id"]), user=ANA)

    chat.delete_folder(f["id"], user=ANA)
    assert f["id"] not in {x["id"] for x in chat.folders(user=ANA)["folders"]}
    row = next(c for c in chat.conversations(user=ANA) if c["id"] == cid)
    assert row["folder_id"] is None                        # unfiled, not deleted
