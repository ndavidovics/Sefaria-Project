"""
translation_request.py
Writes to MongoDB Collection: requests
"""
from datetime import datetime, timedelta

from . import abstract as abst
from . import text
from . import history
from sefaria.system.database import db
from sefaria.system.exceptions import InputError


class TranslationRequest(abst.AbstractMongoRecord):
    """
    A Request for a section of text to be translated.
    """
    collection   = 'translation_requests'
    history_noun = 'translationRequest'

    required_attrs = [
        "ref",             # string ref
        "requesters",      # list of int uids
        "request_count",   # int of requesters length
        "completed",       # bool
        "first_requested", # date
        "last_requested",  # date
    ]
    optional_attrs = [
        "completed_date",  # date
        "completer",       # int uid
    ]

    def _init_defaults(self):
        self.requesters = []
        self.completed  = False

    def save(self):
        self.requesters    = list(set(self.requesters))
        self.request_count = len(self.requesters)
        super(TranslationRequest, self).save()

    def _normalize(self):
        self.ref = text.Ref(self.ref).normal()

    def check_complete(self):
        """
        Checks if this Request has been fulfilled,
        mark and save if so.
        """
        oref = text.Ref(self.ref)
        if oref.is_text_translated():
            self.completed      = True
            self.completed_date = datetime.now()
            logs                = history.HistorySet({"ref": self.ref, "rev_type": "add text", "language": "en"})
            self.completer      = logs[-1].user if logs else None
            self.save()
            return True
        return False

    def contents(self):
        contents = super(TranslationRequest, self).contents()
        contents["first_requested"] = contents["first_requested"].isoformat()
        contents["last_requested"]  = contents["last_requested"].isoformat()
        return contents

    @staticmethod
    def make_request(tref, uid):
        """
        Updates existing TranslationRequest for tref with uid if present,
        creates a new object if not.
        """
        tr = TranslationRequest().load({"ref": tref})
        if tr:
            tr.requesters.append(uid)
            tr.last_requested = datetime.now()
        else:
            tr = TranslationRequest({"ref": tref, "requesters": [uid]})
            tr.first_requested = datetime.now()
            tr.last_requested  = datetime.now()
        tr.save()
        return tr

    @staticmethod
    def remove_request(tref, uid):
        """
        Remove uid from TranslationRequest for tref if there are other requesters,
        delete TranslationRequest if not.
        """
        tr = TranslationRequest().load({"ref": tref})
        if tr:
            tr.requesters.remove(uid)
            if len(tr.requesters):
                tr.save()
            else:
                tr.delete()


class TranslationRequestSet(abst.AbstractMongoSet):
    recordClass = TranslationRequest

    def __init__(self, query={}, page=0, limit=0, sort=[["request_count", 1]]):
        super(TranslationRequestSet, self).__init__(query, page, limit, sort)


def add_translation_requests_from_source_sheets(hours=0):
    """
    Walks through all source sheets, checking for included refs that are untranslated.
    Adds the user ID of the sheet owner as a request for each untranslated ref.
    
    Only consider the last 'hours' of modified sheets, unless 
    hours = 0, then consider all.
    """
    if hours == 0:
        query = {}
    else:
        cutoff = datetime.now() - timedelta(hours=hours)
        query = { "dateModified": { "$gt": cutoff.isoformat() } }

    sheets = db.sheets.find(query)
    for sheet in sheets:
        for ref in sheet.get("included_refs", []):
            if not ref:
                continue
            try:
                r = text.Ref(ref)
                if not r.is_text_translated():
                    TranslationRequest.make_request(ref, sheet["owner"])
            except InputError:
                continue


def process_version_state_change_in_translation_requests(version, **kwargs):
    """
    When a version is updated, check if Translation Requests have been fullfilled.
    """
    requests = TranslationRequestSet({"ref": {"$regex": text.Ref(version.title).regex()}})
    for request in requests:
        request.check_complete()