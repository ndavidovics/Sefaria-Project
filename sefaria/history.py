"""
history.py - managing the revision/activity history.

Write to MongoDB collection: history
"""

from datetime import datetime
from diff_match_patch import diff_match_patch
from bson.code import Code

from sefaria.model import *
#from sefaria.utils.util import *
from sefaria.system.database import db

dmp = diff_match_patch()


def get_activity(query={}, page_size=100, page=1, filter_type=None):
    """
    Returns a list of activity items matching query,
    joins with user info on each item and sets urls.
    """
    query.update(filter_type_to_query(filter_type))
    activity = list(db.history.find(query).sort([["date", -1]]).skip((page - 1) * page_size).limit(page_size))

    for i in range(len(activity)):
        a = activity[i]
        if a["rev_type"].endswith("text") or a["rev_type"] == "review":
            try:
                a["history_url"] = "/activity/%s/%s/%s" % (Ref(a["ref"]).url(), a["language"], a["version"].replace(" ", "_"))
            except:
                a["history_url"] = "#"
    return activity


def text_history(tref, version, lang, filter_type=None):
    """
    Return a complete list of changes to a segment of text (identified by ref/version/lang)
    """
    tref = Ref(tref).normal()
    refRe = '^%s$|^%s:' % (tref, tref)
    query = {"ref": {"$regex": refRe}, "version": version, "language": lang}
    query.update(filter_type_to_query(filter_type))

    return get_activity(query, page_size=0, page=1, filter_type=filter_type)


def filter_type_to_query(filter_type):
    """
    Translates an activity filter string into a query that searches for it.
    Most strings search for filter_type in the rev_type field, but others may have different behavior:

    'translate' - version is SCT and type is 'add text'
    'flagged'   - type is review and score is less thatn 0.4
    """
    q = {}

    if filter_type == "translate":
        q = {"$and": [dict(q.items() + {"rev_type": "add text"}.items()), {"version": "Sefaria Community Translation"}]}
    elif filter_type == "index_change":
        q = {"rev_type": {"$in": ["add index", "edit index"]}}
    elif filter_type == "flagged":
        q = {"$and": [dict(q.items() + {"rev_type": "review"}.items()), {"score": {"$lte": 0.4}}]}
    elif filter_type:
        q["rev_type"] = filter_type.replace("_", " ")

    return q


def collapse_activity(activity):
    """
    Returns a list of activity items in which edits / additions to consecutive segments are collapsed
    into a single entry.
    """

    def continues_streak(a, streak):
        """Returns True if 'a' continues the streak in 'streak'"""
        if not len(streak):
            return False
        b = streak[-1]

        try:
            if a["user"] != b["user"] or \
                a["rev_type"] not in ("edit text", "add text") or \
                b["rev_type"] not in ("edit text", "add text") or \
                a["version"] != b["version"] or \
                Ref(a["ref"]).section_ref() != Ref(b["ref"]).section_ref():

                return False
        except:
            return False

        return True

    def collapse_streak(streak):
        """Returns a single summary activity item that collapses 'streak'"""
        if not len(streak):
            return None
        if len(streak) == 1:
            return streak[0]

        act = streak[0]
        act.update({
            "summary": True,
            #"contents": streak[1:],
            # add the update count form first item if it exists, in case that item was a sumamry itself
            "updates_count": len(streak) + act.get("updates_count", 1) -1,
            "history_url": "/activity/%s/%s/%s" % (Ref(act["ref"]).section_ref().url(),
                                                   act["language"],
                                                   act["version"].replace(" ", "_")),
        })
        return act

    collapsed = []
    current_streak = []

    for a in activity:
        if continues_streak(a, current_streak): # The current item continues
            current_streak.append(a)
        else:
            if len(current_streak):
                collapsed.append(collapse_streak(current_streak))
            current_streak = [a]

    if len(current_streak):
        collapsed.append(collapse_streak(current_streak))

    return collapsed


def get_maximal_collapsed_activity(query={}, page_size=100, page=1, filter_type=None):
    """
    Returns (activity, page) where
    activity is the collasped set of activity items, counting multiple consecutive actions as one
    page is the page number for the next page of queries to search, or None if there are no more results.

    Makes repeat DB calls to return more activity items so a full page_size of items cen returned.
    """
    activity = get_activity(query=query, page_size=page_size, page=page, filter_type=filter_type)
    enough = False
    if len(activity) < page_size:
        enough = True
        page = None

    activity = collapse_activity(activity)

    if len(activity) >= page_size:
        enough = True

    while not enough:
        page += 1
        new_activity = get_activity(query=query, page_size=page_size, page=page, filter_type=filter_type)
        if len(new_activity) < page_size:
            page = None
            enough = True
        activity = collapse_activity(activity + new_activity)
        enough = enough or len(activity) >= page_size # don't set enough to False if already set to True above

    return (activity, page)


def text_at_revision(tref, version, lang, revision):
    """
    Returns the state of a text (identified by ref/version/lang) at revision number 'revision'
    """
    changes = db.history.find({"ref": tref, "version": version, "language": lang}).sort([['revision', -1]])
    current = TextChunk(Ref(tref), lang, version)
    text = unicode(current.text)  # needed?

    for i in range(changes.count()):
        r = changes[i]
        if r["revision"] == revision: break
        patch = dmp.patch_fromText(r["revert_patch"])
        text = dmp.patch_apply(patch, text)[0]

    return text


def next_revision_num():
    """
    Deprecated in favor of sefaria.model.history.next_revision_num()
    """
    last_rev = db.history.find().sort([['revision', -1]]).limit(1)
    revision = last_rev.next()["revision"] + 1 if last_rev.count() else 1
    return revision


def record_sheet_publication(sheet_id, uid):
    """
    Records the publications of a new Source Sheet.
    """
    log = {
        "user": uid,
        "sheet": sheet_id,
        "date": datetime.now(),
        "rev_type": "publish sheet",
    }
    db.history.save(log)


def delete_sheet_publication(sheet_id, user_id):
    """
    Deletes the activity feed item for a sheet publication
    (for when a user unpublishes a sheet)
    """
    db.history.remove({
            "user": user_id,
            "sheet": sheet_id,
            "rev_type": "publish sheet"
        })


def top_contributors(days=None):
    """
    Returns a list of users and their activity counts, either in the previous
    'days' if present or across all time.
    Assumes counts have been precalculated and stored in the DB.
    """
    if days:
        collection = "leaders_%d" % days
    else:
        collection = "leaders_alltime"

    leaders = db[collection].find().sort([["count", -1]])

    return [{"user": l["_id"], "count": l["count"]} for l in leaders]


def make_leaderboard_condition(start=None, end=None, ref_regex=None, version=None, actions=[], api=False):

    condition = {}

    # Time Conditions
    if start and end:
        condition["date"] = { "$gt": start, "$lt": end }
    elif start and not end:
        condition["date"] = { "$gt": start }
    elif end and not start:
        condition["date"] = { "$lt": end }

    # Regular Expression to search Ref
    if ref_regex:
        condition["ref"] = {"$regex": ref_regex}

    # Limit to a specific text version
    if version:
        condition["version"] = version

    # Count acitvity from API?
    if not api:
        condition["method"] = {"$ne": "API"}

    return condition


def make_leaderboard(condition):
    """
    Returns a list of user and activity counts for activity that
    matches the conditions of 'condition' - an object used to query
    the history collection.

    This fucntion queries and calculates for all currently matching history.
    """

    reducer = Code("""
                    function(obj, prev) {

                        // Total Points
                        switch(obj.rev_type) {
                            case "add text":
                                if (obj.language !== 'he' && obj.version === "Sefaria Community Translation") {
                                    prev.count += Math.max(obj.revert_patch.length / 10, 10);
                                    prev.translateCount += 1
                                } else if(obj.language !== 'he') {
                                    prev.count += Math.max(obj.revert_patch.length / 400, 2);
                                    prev.addCount += 1
                                } else {
                                    prev.count += Math.max(obj.revert_patch.length / 800, 1);
                                    prev.addCount += 1
                                }
                                break;
                            case "edit text":
                                prev.count += Math.max(obj.revert_patch.length / 1200, 1);
                                prev.editCount += 1
                                break;
                            case "revert text":
                                prev.count += 1;
                                break;
                            case "review":
                                prev.count += 15;
                                prev.reviewCount += 1;
                                break;
                            case "add index":
                                prev.count += 5;
                                break;
                            case "edit index":
                                prev.count += 1;
                                prev.editCount += 1
                                break;
                            case "add link":
                                prev.count += 2;
                                prev.linkCount += 1;
                                break;
                            case "edit link":
                                prev.editCount += 1
                                prev.count += 1;
                                break;
                            case "delete link":
                                prev.count += 1;
                                break;
                            case "add note":
                                prev.count += 1;
                                prev.noteCount += 1;
                                break;
                            case "edit note":
                                prev.count += 1;
                                break;
                            case "delete note":
                                prev.count += 1;
                                break;
                        }

                        // Texts worked on
                        var refs = []
                        if ("ref" in obj && obj.ref) {
                            refs.push(obj.ref);
                        } else if ("refs" in obj && obj.refs[0] && obj.refs[1]) {
                            refs.push(obj.refs[0]);
                            refs.push(obj.refs[1]);
                        }
                        refs.forEach(function(ref) {
                            var text = ref;
                            var i = text.search(/\d/);
                            var text = text.slice(0,i).trim()

                            if (prev.texts[text]) {
                                prev.texts[text] += 1;
                            } else {
                                prev.texts[text] = 1;
                            }
                        });
                    }
                """)

    leaders = db.history.group(['user'],
                        condition,
                        {
                            'count': 0,
                            'translateCount': 0,
                            'addCount': 0,
                            'editCount': 0,
                            'linkCount': 0,
                            'noteCount': 0,
                            'reviewCount': 0,

                            'texts': {}
                        },
                        reducer)

    return sorted(leaders, key=lambda x: -x["count"])

