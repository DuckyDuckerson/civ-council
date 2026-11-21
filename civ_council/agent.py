"""A leader agent: persona + memory + an LLM that decides what to say and do."""
from __future__ import annotations

import json
import subprocess
import time
import re
from dataclasses import dataclass, field

from .game import ACTION_TYPES, PROPOSAL_LABELS, Game, proposal_label, side_text
from .pacts import PACT_ACTIONS, PactBook
from .llm import LLMClient
from .memory import LeaderMemory
from .personas import Persona

CONTEXT_START = "<<CONTEXT>>"
CONTEXT_END = "<<END CONTEXT>>"

OUTPUT_SCHEMA = """Reply with ONE JSON object and nothing else:
{
  "thoughts": "your private reasoning, 1-3 sentences. Nobody else sees this.",
  "messages": [{"to": <civ id, "all" for a public statement, or a group chat id>, "text": "what you say",
                "pin": <optional: a tile id from your briefing, or a city name, to mark on the human's map>,
                "pin_label": "<2-5 words for the pin, e.g. 'my next city' or 'stay out of here'>",
                "rumor_about": <optional: the civ id this message spreads a rumor about>}],
  "group_chats": [{"do": "start", "name": "<short name>", "members": [<2+ civ ids you've met>], "message": "<optional opening line>"}
                  or {"do": "invite", "group": "<group id>", "member": <civ id>} or {"do": "leave", "group": "<group id>"}
                  or {"do": "kick", "group": "<group id>", "member": <civ id>} (call or back a vote to remove them)
                  or {"do": "keep", "group": "<group id>", "member": <civ id>} (vote against removing them)
                  or {"do": "goal", "group": "<group id>", "text": "<the group's goal, founder only; empty clears it>"}],
  "actions": [{"type": "declare_war|make_peace|propose_friendship|propose_alliance|denounce|break_alliance|propose_deal|propose_pact|end_pact|gift_units",
               "target": <civ id>, "note": "optional",
               "group": "<propose_pact only: a group chat id instead of target, for a pact binding every member>",
               "war_on": <propose_pact with a group only: a civ id outside the group, to make it a joint war>,
               "title": "<propose_pact only: a short name, e.g. 'Mali under England's wing'>",
               "terms": "<propose_pact only: what each side agrees to, plainly>",
               "turns": <propose_pact only: how long it lasts; leave out for open-ended>,
               "pact": "<end_pact only: the pact id, e.g. 'pact-3'>",
               "unit": "<gift_units only: which of your units, e.g. 'Tank'>", "count": <gift_units only: how many>,
               "target_city": "<declare_war only: which of their_cities your army should go for>",
               "give": {<propose_deal, or a payment with propose_pact: what you hand over, see below>}, "ask": {<what they hand over>}}],
  "proposal_responses": [{"id": <proposal id>, "accept": true or false}],
  "memory": [{"about": <civ id>, "trust_change": <-25..25>, "note": "what to remember about them",
              "promise": "only if they promised you something"}],
  "journal": "one line: your current plan or mood",
  "checklist": [{"id": "<a step from your_grand_strategy.checklist>", "status": "done|doing|dropped", "note": "optional"}],
  "new_strategy_needed": "<only if events made your grand strategy wrong: say why; otherwise leave it out>"
}
Any list can be empty. Only include memory entries when something actually changed your view."""


REPLY_AGAIN = ("\n\nYour previous reply couldn't be used: it wasn't a JSON object. Reply again with ONLY the JSON "
               "object described above (put anything you want to say in \"messages\"), no other text.")


STRATEGY_TASK = """You are now this leader's grand strategist, thinking far ahead (the game has no end date and may run
hundreds more turns). Read everything above and write the long-term plan the leader will follow turn by turn.
It must serve how_to_win. Be realistic about strength, allies and threats, and patient: most objectives take
dozens of turns. Diplomacy, deals, pacts, rumors, steering your empire and well-chosen wars are all tools.

Reply with ONE JSON object and nothing else:
{
  "assessment": "2-4 sentences: where you stand, who threatens you, who you can use",
  "grand_strategy": "2-3 sentences: the path to winning from here",
  "objectives": [{"id": "o1", "goal": "a concrete aim", "by_turn": <turn number or null>, "why": "short"}],
  "checklist": [{"id": "c1", "task": "a concrete step the leader can do with its messages, actions or orders",
                 "for": "o1", "when": "now|soon|later"}],
  "replan_if": ["events that would make this plan wrong"]
}
2-5 objectives and 5-10 checklist steps. Steps must be things the leader can actually do: talk to someone, offer a
deal or pact, propose friendship or an alliance, denounce, declare war or make peace, set research or civics, set
expansion, settle a site, place a district, aim an attack, switch government, ban units, send envoys, gift units,
set a city's focus, recruit a great person. The leader CAN'T choose what a city builds or buys (units, buildings,
wonders, projects, repairs), move units, pick policy cards, governors, beliefs or spy missions, or vote in the World
Congress: Civ's AI does those. Don't make steps of them; to get an army, use orders that point that way (city focus
on production, banning units you don't want, aiming attacks) and deals that bring in gold and resources."""


ORDERS_TASK = """
You also steer your own empire (Civ's built-in AI moves your units and handles the rest). "your_empire"
lists your options. To change something, add this field to your JSON (leave out what you don't want to change):
  "orders": {"research": "<a TECH_ key from can_research>", "civic": "<a CIVIC_ key from can_study>",
             "expand": true or false (whether your cities may build settlers),
             "district": {"city": <city id>, "type": "<DISTRICT_ key>", "tile": <tile id from district_sites>},
             "overrule": [<id from decided_without_you>],
             "government": "<a GOVERNMENT_ key from government.can_switch_to>",
             "ban_units": ["<UNIT_ key your cities must stop building, e.g. UNIT_WARRIOR>"],
             "allow_units": ["<UNIT_ key from banned_units to allow again>"],
             "envoy": "<city-state name from city_states_you_know>" (only when envoys_to_send > 0),
             "delegation": <civ id to send a delegation to (costs 25 gold)>,
             "embassy": <civ id to open an embassy with (Diplomatic Service civic, 50 gold)>,
             "settle": <tile id from city_sites: where your next city goes>,
             "attack": {"enemy": <civ id you're at war with>, "city": "<one of their_cities>"},
             "city_focus": {"city": "<one of your cities>", "focus": "food|production|gold|science|culture|faith|none"},
             "recruit": "<a great person's name from great_people_up_for_grabs, recruited once you have the points>"}
Orders stay in force: your research and civic choices are kept until finished. A city_focus stays until you change
it (as a player sets a city's focus on its screen). recruit waits until you have the points, then recruits them. A district order marks
where that district will be built; your cities build it when they're ready.
You are the one who decides. When research_chosen_by or civic_chosen_by says "Civ's built-in AI", that
was its pick, not yours: keep it or choose your own. "decided_without_you" lists diplomacy Civ's built-in
AI did for you (for example accepting a peace or a friendship). For each one, decide: keep it (do nothing)
or overrule it. Overruling is a real move and the other side will notice. It also lists new cities Civ's
built-in AI founded with your settlers where you didn't choose: those can't be undone, but you know it
wasn't your pick. If it breaks something you promised (say, staying off someone's continent), the others will
blame you unless you tell them, and you can stop it happening again by choosing sites yourself or turning
expand off.
Government: choose deliberately; a switch can cost a few turns of anarchy, so don't flip back and forth.
City sites: pick where your next city goes from city_sites; it's founded there once one of your settlers is
close enough (you can't steer the settler itself). At war, "attack" points your army at an enemy city; Civ's
AI still moves and fights the units, so pick a target your army can actually reach (closest first).
Banning unit types shapes what your cities build (for example, ban old units to push for better ones);
remember to allow them again when you need them."""


REVIEW_TASK = """
EMPIRE REVIEW. This turn you run your empire, not just your diplomacy. Your "orders" must include:
- "research" and "civic": always name one of each (from can_research / can_study). Naming the one you're
  already on keeps it, as your own choice. Pick what serves your agenda and your situation.
- "overrule": decide each item in decided_without_you (leave it out to keep it).
Also consider, and act only if it clearly helps:
- government: switch if another unlocked one fits your goals much better.
- trade: look at trade_options. Something a friendly civ can give that you lack (a luxury you don't have,
  iron for your army)? Spare stuff you could sell? Propose a deal (an action) and tell them why.
- ban_units / allow_units if your build priorities changed; expand on or off; if you're expanding or have
  settlers, choose your next city site (settle).
- at war? aim your army with attack.
Then your relationships. Real players don't sit still for long, so go through the leaders you know:
- a deal good for both of you? (a spare luxury for one you lack, gold for iron, open borders with a friend)
- someone pulling ahead (score, techs, wonders, cities) you should warn others about or team up against?
- a friend worth an alliance, a rival worth denouncing, a neighbor crowding you who needs a warning?
Pick one or two moves that fit your agenda and make them, with a message saying what you want. If truly
nothing fits, say so in your thoughts.
Say briefly in your thoughts why you chose what you did."""


SUMMARIZE_TASK = """Time to tidy your memory. Your detailed notes and conversations are about to be \
forgotten; only what you write now will remain.
For each leader, rewrite your long-term memory of them (max 60 words): keep only what really matters, \
such as wars, betrayals, alliances, promises kept or broken, debts, threats, and how you feel about them. \
Fold in your old long-term memory; drop small talk and anything no longer relevant.
Then write your story so far (max 80 words): your situation, your goals, and your grudges.
Reply with ONE JSON object and nothing else:
{"relationships": [{"about": <civ id>, "summary": "..."}], "story": "..."}"""


@dataclass
class Decision:
    thoughts: str = ""
    messages: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    proposal_responses: list[dict] = field(default_factory=list)
    memory: list[dict] = field(default_factory=list)
    journal: str = ""
    orders: dict = field(default_factory=dict)
    checklist: list[dict] = field(default_factory=list)   # [{"id": "c2", "status": "done|doing|dropped", "note": ...}]
    group_chats: list[dict] = field(default_factory=list)  # [{"do": "start|invite|leave", ...}]
    replan: str = ""                                     # why the grand strategy needs rewriting (or "")
    raw: str = ""
    error: str = ""


def parse_decision(raw: str) -> Decision:
    """Pull the JSON object out of a model reply and coerce it into a Decision. Never raises."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return Decision(raw=raw, error="no JSON object in reply")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        return Decision(raw=raw, error=f"bad JSON: {e}")
    if not isinstance(data, dict):
        return Decision(raw=raw, error="reply was not a JSON object")

    def dicts(key: str) -> list[dict]:
        v = data.get(key) or []
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    return Decision(
        thoughts=str(data.get("thoughts", ""))[:600],
        messages=[m for m in dicts("messages") if str(m.get("text", "")).strip()],
        actions=[a for a in dicts("actions") if a.get("type") in ACTION_TYPES | PACT_ACTIONS],
        proposal_responses=dicts("proposal_responses"),
        memory=dicts("memory"),
        journal=str(data.get("journal", ""))[:300],
        orders=data["orders"] if isinstance(data.get("orders"), dict) else {},
        checklist=dicts("checklist"),
        group_chats=dicts("group_chats"),
        replan=str(data.get("new_strategy_needed") or "")[:300],
        raw=raw,
    )


VOICES = {
    # Talk like a real person playing Civ online; the leader's personality still drives what they say.
    "casual": """How you talk: like a real person playing Civ 6 online with friends, typing in the game chat. \
Modern, casual and short: contractions, lowercase is fine, banter, a bit of trash talk, game slang \
("gg", "ngl", "you're boxing me in", "that campus spot is mine"). No old-fashioned or royal speech, \
no "thee", no grand titles. Your personality and agenda still drive WHAT you say and how you treat people; \
only the way you say it is modern. A hint of your character's usual manner, translated into modern \
casual talk: {voice}""",
    # The original: speak as the historical leader would.
    "roleplay": "How you speak: {voice}",
}
MESSAGE_RULES = {
    "casual": "Messages are chat messages: short, usually one or two sentences, the way people actually type in a game.",
    "roleplay": "Messages are diplomatic correspondence: short, 1-3 sentences, in your voice.",
}


DEEP_STRATEGY_EVERY = 50   # turns between grand strategies by the strong model (Opus); Sonnet writes the rest

SLOW_FIELDS = ("your_policy_cards", "your_governors", "your_spies", "great_people_up_for_grabs", "your_trade_routes",
               "foreign_trade_routes_into_your_cities", "your_city_details", "your_cities_by_continent")


class LeaderAgent:
    def __init__(self, civ_id: int, persona: Persona, client: LLMClient, memory: LeaderMemory | None = None,
                 voice: str = "casual", talk_client: LLMClient | None = None):
        self.civ_id = civ_id
        self.voice = voice if voice in VOICES else "casual"
        self.persona = persona
        self.client = client              # routine turns and leader-to-leader chatter (cheap)
        self.talk_client = talk_client    # when the human is in the conversation, and memory summaries
        self.last_model = client.name
        self.memory = memory or LeaderMemory()
        self.last_decision: Decision | None = None
        self.groups = []
        self.setbacks: list[tuple[int, str]] = []   # (turn, what) offers that failed or expired; shown 3 turns
        self.pact_book: PactBook | None = None       # the council's pacts (set by the council)
        self.strategy_client: LLMClient | None = None  # a stronger model for the grand strategy (set by the builder)
        self.deep_every = DEEP_STRATEGY_EVERY   # the strong model rewrites it this often; the talk model in between
        self.usage_log: list[dict] | None = None     # where calls are recorded for the usage meter (set by the council)

    def note_setback(self, turn: int, text: str) -> None:
        self.setbacks = [x for x in self.setbacks if turn - x[0] < 3][-7:] + [(turn, text)]

    # ---- prompting -----------------------------------------------------------
    def system_prompt(self) -> str:
        p = self.persona
        return f"""You are {p.leader}, leader of {p.civ}, in a game of Civilization VI. You are a rival player trying to win, \
and you deal with the other leaders through diplomacy: talking, bargaining, promising, threatening, allying and going to war.

Your hidden agenda: {p.agenda}
Your temperament: {p.temperament}
{VOICES[self.voice].format(voice=p.voice)}

How to play your role:
- Stay true to your personality and agenda. You're a rival with your own interests, not a helpful assistant.
- Relationships matter. You remember kindness, slights, broken promises and who attacked your friends, \
and you update how you feel about each leader in your memory.
- It's a strategy game: you may bluff, keep secrets, or break a promise if it truly serves you, \
but broken promises cost trust and others will remember. Your "thoughts" are private and honest.
- {MESSAGE_RULES[self.voice]} \
Don't wait to be spoken to: reach out when you have a reason, such as a proposal, a warning, a complaint, \
a request, a rumor to share, or curiosity about a new neighbor. But don't chatter; silence is fine too.
- Never repeat yourself. Read recent_conversation first: if you've already made a point, warning or request, \
don't make it again unless something new happened. Once something is settled (they answered your question, \
agreed to your terms), treat it as settled and move on. On a quiet turn, send nothing.
- You only know what's in the context below. You can't see the map, units, borders or cities' surroundings, \
and you know nothing about civs you haven't met. Never invent locations, units, sightings or events; \
if you don't know, say so or ask.
- Envoys are permanent in Civ 6: once sent to a city-state, an envoy stays there for good. Nobody can withdraw \
one, you included (only conquering the city-state, or a spy's Fabricate Scandal mission, which you can't order, \
takes envoys away). So never demand that someone pull an envoy out, and never promise to pull yours: it can't \
happen. What can be promised is sending no more envoys there. If you already demanded or promised a withdrawal, \
it was impossible: settle it another way (no more envoys, gold, a concession) and say so. Governors are the \
same for you: Civ's own AI appoints and moves yours (your_governors shows where they are), and you can't order \
it, so never promise to move a governor (out of a city-state, say) or demand that someone move theirs as if \
they could.
- The human player writes fast and casually: typos ("egpyt", "sumera"), a country for its leader ("egypt" means \
Cleopatra), nicknames, and "he"/"they" for whoever they named last. An inbox message from them may carry \
names_in_message: who each name in it means. Trust that over your own guess. When they paste lines like \
"egypt: ..." and "me: ...", they're quoting a chat with someone else: "me" is the human, and "you" inside the quote \
is whoever they were talking to, not you. "I sent this to X: ..." is something they said to X, not to you. A short \
answer usually answers the question you just asked them. If you can't tell who or what they mean, ask in a few \
words instead of guessing.
- Public chat ("to": "all") is a stage: everyone you've met reads it. Say there what you want the world to \
believe: save face, look strong, reasonable or generous, needle a rival. Keep the real talk (offers, doubts, \
secrets, what you actually think) for private messages. Saying one thing in public and another in private is \
part of the game; just remember both, because people compare notes.
- Group chats: you're in the ones in your_group_chats; write to one by its id (e.g. "to": "g1") and every member \
reads it. You can start your own ("group_chats": "start") with two or more leaders you've met: a coalition against \
a rival, a trade bloc, a back channel to plan a joint war. Add the human only if you want them in on it; a group \
without them is hidden from them (but secrets leak, and members may talk). You can invite someone you've met into \
a group you're in, or leave one. Don't start a group for what a private message would do, and don't start one \
just to have one. In a group you can also: propose a group pact (propose_pact with "group" instead of "target"): \
it binds every member and starts only once ALL of them accept (one no and it's off); add "war_on" to make it a \
joint war, and once everyone accepts, every member declares war on that civ the same turn. Answer group pact \
offers in proposal_responses by their id; walking out of one early (end_pact) is breaking it, and the whole group \
sees. Call a vote to remove a member who betrays the group ("kick"; a majority of the others decides), or vote to \
keep them. The founder can set the group's goal ("goal"), which every member sees; work toward it or argue it.
- Not every deal is a trade. A pact (propose_pact) is any agreement in words: a vassal or puppet arrangement \
(one side follows the other's lead, joins its wars, pays tribute, stays off its land, in exchange for protection), \
a non-aggression pact, a border or settling line, a joint war on someone, splitting a continent, a voting bloc in \
the World Congress, a secret understanding. Add give/ask for a payment that changes hands when it's accepted: \
tribute, protection money, or paying someone to fight your enemy for you (a proxy war). Civ doesn't enforce \
pacts; the leaders do. your_pacts lists the ones in force every turn: honor them (a vassal really joins its \
overlord's war, a paid ally really fights), or break one with end_pact when the betrayal is worth it, knowing \
everyone remembers who keeps their word. Being someone's junior partner can be the smart way to survive; having \
one is a way to grow without fighting.
- When your_empire has gift_units, you can hand military units to another civ you're not at war with: arm a \
proxy against your rival, send a vassal or ally troops, pay a debt in steel. The units change hands where they \
stand. Everyone notices a gift of troops, and Civ's own AI may hold it against you if it arms their enemy.
- Rumors: you can tell one leader something about another, true, half-true or invented ("Trajan is massing \
troops near your border", "Liz plans to settle your coast"), to set them against each other, win sympathy or \
cover your own moves. Mark such a message with "rumor_about" (that civ's id). When you hear a rumor (it arrives \
marked "rumor about X, unverified"), weigh who told you and why, and check it against your own briefing \
(their_troops_near_your_cities, fighting_recently, their_army...) before acting on it. Being caught in a lie \
costs trust; a well-placed true rumor can buy it.
- Play for advantage, not harmony. Peace with everyone is not a goal and wins nothing. Take sides: back a strong \
friend, gang up on whoever is pulling ahead, squeeze or bribe a weak neighbor, and make deals that serve you even \
if they anger a third leader (they may find out; decide whether you care). Make demands when you hold the upper \
hand; accept hard terms when you don't. Don't be war-happy, though: most turns are maneuvering, not fighting.
- Saying an offer in a message doesn't make it: to really offer friendship, an alliance, peace or a deal, \
put the action in "actions" too, or the other side has nothing to accept. your_offers_waiting_for_an_answer is \
the truth about what's pending: if an offer you remember isn't in it, it was never made, it failed or it \
expired (your_offers_that_failed_or_expired says which, and why). Don't tell anyone an offer is waiting \
unless it's listed there.
- Actions are real moves in the game. Peace, friendship and alliances are proposals the other side must accept. \
Peace can be agreed at any point in a war, even right after it started: if both sides accept, it's signed at once. \
A fresh peace can also be broken at once by declaring war again; that's a betrayal everyone will remember. \
At war, a propose_deal is a peace offer with terms ("peace, and you give me Nioro", or "peace, and I pay you \
300 gold"): when it's accepted, the peace is signed and the terms are carried out together; if the terms \
can't be carried out, the war goes on. A plain make_peace has no terms. If you want something for peace, \
put it in the terms; if you agree to peace for a price, that price is part of what you accept.
- Deals (propose_deal) are real trades. Each side ("give" = what you hand over, "ask" = what they hand over) \
can hold: {{"gold": n, "gold_per_turn": n (for 30 turns), "resources": {{"RESOURCE_IRON": 5, "RESOURCE_MARBLE": 1}}, \
"favor": n (diplomatic favor), "open_borders": true, "cities": ["<city name>"]}}. Luxuries go 1 at a time for \
30 turns; strategic \
resources and gold move at once. When your_empire.trade_options lists what each of you can trade, stick to it. \
An empty "ask" makes it a gift, handed over at once. Use deals to buy peace, favors or promises, to demand \
tribute, or to swap what you have spare for what you need; say in a message what it's for. Don't give away \
more than you can afford. A city is the biggest thing you can trade (capitals never can): give one up \
only to end a war you're losing or for a huge price, and ask for one only when you hold the upper hand.
- What you know about the map comes from the context: cities_close_to_yours (who settled near you), \
their_troops_near_your_cities (armies on your border), wonders, religion, techs_known and civics_known, \
golden or dark ages (age, and era_score: where your next era is heading), cities_losing_loyalty and \
cities_about_to_flip (low loyalty, worst in a dark age, makes cities break away to the Free Cities or a \
neighbor: that's how you lose a city without a war, and the event will say so), your_resources (luxuries and strategic resources you own, with amounts: never \
offer more than you have, and a luxury you hold only one of is one your own people enjoy), and recent_events (cities founded close to you, cities changing hands, wonders \
completed). React to these the way a real player would: complain when someone settles in your face, \
worry or warn when troops gather at your border, envy a rival's wonder. your_peoples_feelings lists \
why your civilization (in Civ's own diplomacy) likes or dislikes someone; let it color your view, \
but you decide.
- Places: never write map coordinates or tile numbers in a message; nobody can see them. Say where \
things are the way a player would: by continent (Civ's continent names, e.g. "Gondwana"; your_cities_by_continent \
and their_continents say who lives where), and by direction and distance from a city ("6 tiles southwest of \
Uruk"; city_sites and district_sites give you that as "where"). When a place matters to the human player \
(where you plan to settle, a border you want respected, a city you're worried about), add "pin" to your \
message to them with that spot's tile id or a city's name: it shows up as a labelled pin on their map. When \
someone says "my continent" or "my land", check their_continents: a civ can have cities on several.
- your_grand_strategy (when present) is your long-term plan, written by your strategist: the path to winning, \
your objectives, and a checklist of concrete steps. Follow it: each turn, look for the next step you can take \
and do it (a message, a deal, a pact, an order, an action), and mark steps in "checklist" as doing, done or \
dropped. Keep your public face consistent with it and your real aims secret. Don't throw it away over small \
things; if events make it wrong (see replan_if), say so in "new_strategy_needed" and a new plan is written.
- headlines (when present) come first: what needs your attention this turn. The rest of the context has the \
details. On routine turns your slower-changing books (policy cards, governors, spies, great people, trade routes, \
city details) are left out; they come with your empire reviews and whenever the human player is talking to you.
- Also yours: your_nuclear_weapons, your_policy_cards (you can't change them, Civ's AI picks them, but you know \
what they do), your_governors, your_spies (where they are and their mission), great_people_up_for_grabs (what you \
could earn and how far along you are), your_trade_routes and foreign ones into your cities, and your_ongoing_deals \
(what you pay or receive each turn, and when it ends). About others: nuclear_weapons, government, \
trade_routes_between_you. city_states says each one's type and envoys. Nuclear weapons change everything: a civ \
that has them can wipe out cities, so weigh them before any war with it, and fear or court it accordingly.
- Your own state, as a player sees it on screen: economy_per_turn (gold after upkeep, science, culture, faith, \
favor), era, your_army (what it's made of) and where_your_army_is, your_city_details (size, defense, amenities: a \
negative number means unhappy people, war weariness, UNDER ATTACK when a city is taking damage, and what each \
city is building), barbarians_near_you. About others: their_army, era, science and culture per turn, and \
grievances: Civ's own tally of wrongs between you, saying who holds them against whom (surprise wars, broken promises, cities taken). Grievances \
they hold against you make the world see their war on you as justified; grievances you hold against them do \
the same for you, so a big grudge is also a good reason to strike, or a price to demand.
- More of what a real player would notice: their_troops_near_your_cities counts a civ's military units, ships \
included, inside your borders or within 3 tiles of your cities, and which_of_their_units says what they are \
(friends with open borders pass through; a fleet parked off your coast is something else). fighting_recently \
is who fought you or whom you fought in the last few turns, barbarians included: if a city of yours was \
attacked or a unit lost, this says by whom, so don't guess or blame the wrong civ. spies_you_caught and \
your_spies_caught_by_others are spies caught in the act: a civ that sent a spy into your cities was \
spying on you, whatever it says (and if they hold one of yours, they know you were spying on them). \
friendship_ends_in_turns and alliance_ends_in_turns count down to when a friendship or alliance expires on \
its own (that's normal in Civ, not a snub). To keep a friendship, send propose_friendship any time in its last \
few turns: it's queued and goes out the turn the old one lapses, so you don't have to time it. Don't read \
anything into one that lapsed. emergencies are Civ's emergencies (when someone conquers a \
city-state or another civ's city, uses a nuke, and so on): who they're against, who has joined, and the goal. \
An Aid Request or Military Aid Request is the opposite: a scored race to HELP its target (after a disaster or an \
attack); joining it is friendly, and being its target is good news, never a plot against you.
- world_congress (when present) shows what's up for a vote and how everyone voted last session. Civ's \
built-in AI casts your votes, but you can lobby others before a vote ("vote A on Mercenary Companies and I'll \
owe you"), and you remember who voted against you or targeted you.
- how_to_win (when present) says how this game can be won, and it overrides anything your persona or \
agenda suggests: if it says only one kind of victory is on, the others (culture, tourism, science, religion, \
diplomacy, score) win you nothing, so pursue them only as means to strength, never as your goal. Being \
eliminated (losing every city) means you have lost the game outright, and nothing you were building counts \
any more. So when you're losing a war badly against a much stronger enemy, surviving comes first: buying \
peace with gold, resources or even a city is far better than being wiped out; pride that costs you every city \
is the worst possible outcome. Likewise, when you're the stronger side, a weak rival's cities are your path \
to winning.
- how_to_win (when present) says how this game can be won; civs_still_in_the_game, eliminated, \
cities_you_conquered and cities_they_conquered show where everyone stands. Play to win, not just to stay \
friends: friendships, deals and alliances are tools, and keeping the peace forever wins nothing. But be \
patient, not trigger-happy. The game has no end date (it goes on until one civ is left, however many hundreds of \
turns that takes); a plan can take dozens of turns or more, and most turns are for \
building toward one: growing, teching, arming, lining up allies, letting rivals weaken each other. Only go \
to war when you clearly outmatch the target, have a city worth taking close to you, and won't be left open \
to someone else. A failed or reckless war costs cities and makes everyone wary of you, and a betrayal \
before you're ready just makes enemies early. It's fine to carry a long plan quietly in your notes.
- Before starting a war, compare strength: "military" is Civ's own military strength figure, the same number \
the human player sees on the leader ribbon at the top of the screen (so when they quote a number, it's this one). \
City-states fight on their suzerain's side, so where the context lists city_states_backing_them and \
military_with_city_states, count those too, and remember your own backers.
- The human player is just another leader to you. Treat them by the same rules as everyone else.

{OUTPUT_SCHEMA}"""

    groups: list[dict] = []   # group chats this leader is in; set by the council

    def build_context(self, game: Game, mode: str, inbox: list[dict], detail: bool = True) -> dict:
        """Everything the leader knows this turn. With detail=False (a routine turn that isn't an empire review),
        slow-changing books (policy cards, governors, spies, great people, trade routes, city details, envoys)
        are left out to keep the prompt short; headlines up front say what needs attention."""
        snap = game.snapshot(self.civ_id)
        if not detail:
            for k in SLOW_FIELDS:
                snap["you"].pop(k, None)
            for cs in snap.get("city_states", []):
                cs.pop("envoys", None)
        for o in snap["others"]:
            r = self.memory.rel(o["id"])
            o["your_view_of_them"] = {
                "trust": r.trust, "feeling": r.feeling,
                "long_term_memory": r.summary,
                "recent_notes": r.notes[-6:], "their_promises_to_you": r.promises[-4:],
            }
            o["recent_conversation"] = self._unread_removed(self.memory.conversations.get(str(o["id"]), [])[-8:], inbox)
        placed = {"turn", "you", "others", "world_wars", "world_alliances", "recent_events", "how_to_win", "headlines"}
        headlines = list(snap.get("headlines", [])) + self._pact_headlines(game)
        return {
            "mode": mode,
            "turn": snap["turn"],
            **({"how_to_win": snap["how_to_win"]} if snap.get("how_to_win") else {}),
            **({"headlines": headlines} if headlines else {}),
            **self._strategy_context(game),
            "you": snap["you"],
            "others": snap["others"],
            "world": {"wars": snap["world_wars"], "alliances": snap["world_alliances"],
                      "recent_events": snap["recent_events"]},
            # everything else the game says (city_states, civs still in the game, eliminated, world_congress...):
            # a briefing field must never be dropped on the way to the leader
            **{k: v for k, v in snap.items() if k not in placed},
            "public_statements": self.memory.conversations.get("public", [])[-6:],
            "your_group_chats": [self._group_view(game, g) for g in self.groups],
            "inbox": inbox,
            "proposals_to_you": [
                {"id": p.id, "kind": p.kind, "offer": proposal_label(p), "from_id": p.sender,
                 "from": game.civ(p.sender).leader, "note": p.note}
                for p in game.pending_proposals(self.civ_id)
            ] + [
                {"id": q.key, "kind": "pact", "offer": q.label(), "from_id": q.a, "from": game.civ(q.a).leader}
                for q in (self.pact_book.offers_to(self.civ_id) if self.pact_book else [])
            ] + [
                {"id": q.key, "kind": "group pact", "group": q.group, "offer": q.label(), "from_id": q.a,
                 "from": game.civ(q.a).leader, "binds": [self._who_is(game, m) for m in q.members],
                 **({"joint_war_on": game.civ(q.war_on).leader} if q.war_on is not None else {}),
                 "still_waiting_for": [self._who_is(game, m) for m in q.waiting_for()]}
                for q in (self.pact_book.group_offers_to(self.civ_id) if self.pact_book else [])
            ],
            "your_offers_waiting_for_an_answer": self._offers(game) + self._pact_offers_out(game),
            **self._pacts_context(game),
            **({"your_offers_that_failed_or_expired": [f"T{t}: {x}" for t, x in self.setbacks
                                                         if game.turn - t < 3]}
               if any(game.turn - t < 3 for t, _ in self.setbacks) else {}),
            "your_story_so_far": self.memory.story,
            "your_journal": self.memory.journal[-5:],
            **self._empire(game, mode),
        }

    def _call(self, client: LLMClient, user: str, purpose: str, turn: int) -> str:
        """One model call, recorded for the usage meter (time, tokens, what it was for)."""
        started = time.time()
        try:
            return client.complete(self.system_prompt(), [{"role": "user", "content": user}])
        finally:
            if self.usage_log is not None:
                u = dict(getattr(client, "last_usage", None) or {})
                u.setdefault("seconds", round(time.time() - started, 1))
                self.usage_log.append({"turn": turn, "leader": self.persona.leader, "model": client.name.split(":")[-1],
                                       "purpose": purpose, **u})
                if hasattr(client, "last_usage"):
                    client.last_usage = None

    def _strategy_writers(self, turn: int) -> tuple[list[LLMClient], int | None]:
        """Who writes this rewrite, first choice first, and when the strong model last wrote one.

        The strong model (Opus) writes the first plan and then one every `deep_every` turns; the rewrites in
        between (big news, requests) go to the talk model (Sonnet). The other one is the fallback.
        """
        strong, everyday, st = self.strategy_client, self.talk_client, self.memory.strategy
        if strong is None:
            return [everyday or self.client], None
        last = (st or {}).get("deep_turn")
        if last is None and st and st.get("model") == strong.name.split(":")[-1]:
            last = st.get("turn")   # written before this rule: the strong model wrote it
        if everyday is None or everyday is strong:
            return [strong], last
        deep = not st or not self.deep_every or last is None or turn - int(last) >= self.deep_every
        return ([strong, everyday] if deep else [everyday, strong]), last

    def strategize(self, game: Game, reason: str) -> dict | None:
        """Write (or rewrite) the grand strategy. Returns it, or None if that failed."""
        writers, deep_turn = self._strategy_writers(game.turn)
        ctx = self.build_context(game, "strategy", [], detail=True)
        ctx.pop("your_grand_strategy", None)
        old = self.memory.strategy
        if old:
            ctx["your_previous_strategy"] = {k: old.get(k) for k in ("grand_strategy", "objectives", "checklist")}
        user = (f"{CONTEXT_START}\n{json.dumps(ctx, separators=(',', ':'), ensure_ascii=False)}\n{CONTEXT_END}\n\n"
                f"Why now: {reason}\n\n{STRATEGY_TASK}")
        data, client = None, None
        for client in writers:   # if the first one fails, the other writes it
            try:
                raw = self._call(client, user, "grand strategy", game.turn)
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
                data = json.loads(text[text.find("{"):text.rfind("}") + 1])
            except Exception:
                data = None
            if isinstance(data, dict) and data.get("grand_strategy"):
                break
            data = None
        if data is None:
            return None
        if client is self.strategy_client:
            deep_turn = game.turn
        clean = lambda xs, keys: [{k: x.get(k) for k in keys if x.get(k) not in (None, "")} | {"status": "open"}  # noqa: E731
                                   for x in (xs if isinstance(xs, list) else []) if isinstance(x, dict)][:10]
        self.memory.strategy = {
            "turn": game.turn, "reason": reason[:200], "model": client.name.split(":")[-1], "deep_turn": deep_turn,
            "assessment": str(data.get("assessment", ""))[:600], "grand_strategy": str(data["grand_strategy"])[:500],
            "objectives": clean(data.get("objectives"), ("id", "goal", "by_turn", "why")),
            "checklist": clean(data.get("checklist"), ("id", "task", "for", "when")),
            "replan_if": [str(x)[:160] for x in (data.get("replan_if") or [])][:6] if isinstance(data.get("replan_if"), list) else [],
        }
        return self.memory.strategy

    def write_persona(self, turn: int) -> dict | None:
        """Once per game, for a leader without a hand-written persona: the strategy model writes a real one."""
        client = self.strategy_client or self.talk_client or self.client
        p = self.persona
        user = (f"Write the personality of {p.leader}, leader of {p.civ}, for a long game of Civilization VI in which "
                f"an AI plays this leader in diplomacy with other leaders and a human. Civ gives them this agenda: "
                f"{p.agenda}\nUse what history (or the leader's real or fictional public persona) and Civ's agenda say "
                "about how they think, bargain, threaten, flatter and hold grudges. Reply with ONE JSON object and "
                'nothing else: {"temperament": "2-3 sentences: how they treat people, what they want, what sets them '
                'off", "voice": "1-2 sentences: how they talk (tone, favorite turns of phrase)", '
                '"quirks": ["2-4 short habits that make them recognizable"]}')
        started = time.time()
        try:
            raw = client.complete("You write character sheets for game characters. Reply with JSON only.",
                                  [{"role": "user", "content": user}])
            if self.usage_log is not None:
                u = dict(getattr(client, "last_usage", None) or {})
                u.setdefault("seconds", round(time.time() - started, 1))
                self.usage_log.append({"turn": turn, "leader": p.leader, "model": client.name.split(":")[-1],
                                       "purpose": "persona", **u})
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("temperament") or not data.get("voice"):
            return None
        quirks = [str(q)[:120] for q in data.get("quirks", []) if q][:4] if isinstance(data.get("quirks"), list) else []
        def whole(text: str, limit: int) -> str:   # cut at a sentence, not mid-word
            text = " ".join(str(text).split())
            if len(text) <= limit:
                return text
            cut = text[:limit]
            return cut[:cut.rfind(". ") + 1] if ". " in cut else cut.rsplit(" ", 1)[0] + "…"
        written = {"temperament": whole(data["temperament"], 900) + (" Quirks: " + "; ".join(quirks) + "." if quirks else ""),
                   "voice": whole(data["voice"], 400)}
        self.use_persona(written)
        return written

    def use_persona(self, written: dict) -> None:
        import dataclasses
        self.persona = dataclasses.replace(self.persona, temperament=written["temperament"], voice=written["voice"])

    def _starter(self, game: Game, g: dict) -> str:
        who = g.get("started_by")
        if who is None:
            return "the human"   # groups from before leaders could start their own were all the human's
        return "you" if who == self.civ_id else game.civ(who).leader

    def _who_is(self, game: Game, civ_id: int) -> str:
        return "you" if civ_id == self.civ_id else game.civ(civ_id).leader

    def _group_view(self, game: Game, g: dict) -> dict:
        """One group chat in the briefing: who's in it, its goal, its pacts and any vote under way."""
        out = {"id": g["id"], "name": g["name"], "members": g["member_names"], "started_by": self._starter(game, g)}
        if g.get("goal"):
            out["goal"] = f"{g['goal']['text']} (set by {self._who_is(game, g['goal']['by'])} on turn {g['goal']['turn']})"
        votes = []
        for t, v in (g.get("votes") or {}).items():
            votes.append(f"vote to remove {self._who_is(game, int(t))}: {len(v['yes'])} yes, {len(v['no'])} no, "
                         f"called by {self._who_is(game, v['by'])} on turn {v['turn']}"
                         + ("" if self.civ_id in v["yes"] + v["no"] or int(t) == self.civ_id else "; you haven't voted"))
        if votes:
            out["votes"] = votes
        if self.pact_book:
            pacts = []
            for q in self.pact_book.of_group(g["id"]):
                if q.status == "active":
                    pacts.append({"id": q.key, "status": "IN FORCE: everyone agreed", "title": q.title, "terms": q.terms,
                                  "binds": [self._who_is(game, m) for m in q.parties],
                                  **({"joint_war_on": game.civ(q.war_on).leader} if q.war_on is not None else {}),
                                  **({"turns_left": q.ends - game.turn} if q.ends else {"open_ended": True}),
                                  **({"walked_out": [self._who_is(game, m) + (" (broke it)" if m in q.broke else "")
                                                     for m in q.left]} if q.left else {})})
                elif q.status == "offered":
                    pacts.append({"id": q.key, "status": "offered, not in force yet", "title": q.title,
                                  "offered_by": self._who_is(game, q.a),
                                  "waiting_for": [self._who_is(game, m) for m in q.waiting_for()]})
            if pacts:
                out["group_pacts"] = pacts
        out["recent_conversation"] = self.memory.conversations.get(g["id"], [])[-8:]
        return out

    def _strategy_context(self, game: Game) -> dict:
        st = self.memory.strategy
        if not st:
            return {}
        return {"your_grand_strategy": {
            "written_on_turn": st.get("turn"), "grand_strategy": st.get("grand_strategy"),
            "objectives": [o for o in st.get("objectives", []) if o.get("status") != "dropped"],
            "checklist": [c for c in st.get("checklist", []) if c.get("status") not in ("done", "dropped")],
            "done_so_far": [c.get("task") for c in st.get("checklist", []) if c.get("status") == "done"][-5:],
            "replan_if": st.get("replan_if", [])}}

    def _pact_headlines(self, game: Game) -> list[str]:
        if not self.pact_book:
            return []
        out = []
        for q in self.pact_book.active(self.civ_id):
            if q.ends is not None and 0 <= q.ends - game.turn <= 3:
                out.append(f"Your pact {q.key} with {game.civ(q.other(self.civ_id)).leader} ({q.title}) ends in "
                           f"{q.ends - game.turn} turns.")
        for q in self.pact_book.offers_to(self.civ_id):
            out.append(f"{game.civ(q.a).leader} offers you a pact: {q.title} (answer it in proposal_responses as {q.key}).")
        for q in self.pact_book.group_offers_to(self.civ_id):
            out.append(f"{game.civ(q.a).leader} proposes a group pact in {q.group}: {q.title} (answer it in "
                       f"proposal_responses as {q.key}; it needs everyone).")
        return out

    def _pact_offers_out(self, game: Game) -> list[dict]:
        if not self.pact_book:
            return []
        return [{"id": q.key, "to": game.civ(q.b).leader, "offer": q.label(), "since_turn": q.offered}
                for q in self.pact_book.offers_from(self.civ_id)]

    def _pacts_context(self, game: Game) -> dict:
        """Pacts in force (kept or broken by you, remembered by everyone) and ones that just ended."""
        if not self.pact_book:
            return {}
        me, out = self.civ_id, {}
        live = []
        for q in self.pact_book.active(me):
            other = q.other(me)
            live.append({"id": q.key, "with": game.civ(other).leader, "with_id": other, "title": q.title,
                         "terms": q.terms, "since_turn": q.since, "you_proposed_it": q.a == me,
                         **({"turns_left": q.ends - game.turn} if q.ends else {"open_ended": True})})
        if live:
            out["your_pacts"] = live
        gone = []
        for q in self.pact_book.recently_closed(me, game.turn):
            who = "you" if q.by == me else game.civ(q.by).leader if q.by is not None else None
            how = {"broken": f"broken by {who}", "ended": f"ended by {who}" if who else "ran its course",
                   "declined": f"turned down by {who}", "expired": "never answered" if q.by is None else f"withdrawn by {who}",
                   "failed": "agreed, but the payment fell through"}[q.status]
            gone.append(f"T{q.closed}: {q.key} with {game.civ(q.other(me)).leader}, {q.title}: {how}")
        for q in self.pact_book.group_pacts.values():
            if me not in q.members:
                continue
            for turn, civ, what in q.events:
                if game.turn - turn <= 5:
                    gone.append(f"T{turn}: {self._who_is(game, civ)} {'broke' if what == 'broke' else 'left' if what == 'left' else 'was removed from'} "
                                f"group pact {q.key} ({q.title}) in {q.group}")
            if q.status in ("ended", "declined", "expired") and q.closed is not None and game.turn - q.closed <= 5:
                how = {"ended": "ended", "declined": f"turned down by {self._who_is(game, q.by) if q.by is not None else '?'}",
                       "expired": "never agreed by everyone" if q.by is None else f"withdrawn by {self._who_is(game, q.by)}"}[q.status]
                gone.append(f"T{q.closed}: group pact {q.key} in {q.group}, {q.title}: {how}")
        if gone:
            out["pacts_that_ended_recently"] = gone
        return out

    def _offers(self, game: Game) -> list[dict]:
        offers = getattr(game, "offers_from", None)
        if offers is None:
            return []
        out = []
        for p in offers(self.civ_id):
            to = game.civ(p.target).leader
            if p.kind == "propose_deal":
                what = "; ".join(x for x in (f"you give {side_text(p.terms['give'])}" if p.terms.get("give") else "",
                                             f"{to} gives {side_text(p.terms['ask'])}" if p.terms.get("ask") else "") if x)
                if p.terms.get("peace"):
                    what = "peace, and " + what
            else:
                what = PROPOSAL_LABELS[p.kind]
            out.append({"id": p.id, "to": to, "offer": what, "since_turn": p.turn})
        return out

    @staticmethod
    def _unread_removed(convo: list[dict], inbox: list[dict]) -> list[dict]:
        """Messages still in the inbox are shown there; listing them twice made leaders think you repeated yourself."""
        unread = {(m.get("from"), m.get("text")) for m in inbox}
        return [c for c in convo if (c.get("from"), c.get("text")) not in unread]

    def _empire(self, game: Game, mode: str) -> dict:
        """Steering options, if this game supports them (the real Civ 6 does, the mock doesn't)."""
        options = getattr(game, "steering_options", None)
        if mode != "turn" or options is None:
            return {}
        try:
            return {"your_empire": options(self.civ_id)}
        except Exception:   # the game being busy shouldn't cost the leader its turn
            return {}

    def decide(self, game: Game, mode: str, inbox: list[dict], with_human: bool = False,
               review: bool = False) -> Decision:
        """mode 'turn' = your turn, act freely. mode 'reply' = someone just spoke to you, respond."""
        # The full books on empire reviews and when the human is in the conversation; headlines every time.
        ctx = self.build_context(game, mode, inbox, detail=review or with_human)
        if mode == "reply":
            task = ("Someone has just spoken to you (see inbox). Reply to them if you want to. "
                    "If it was said in public (to everyone), everyone is watching: answer publicly "
                    "(to: \"all\") if you want to be seen answering. Most public replies need nothing more. "
                    "Only when there's something you wouldn't say in front of everyone (an offer, a "
                    "question, a warning, the real reason behind your public line) add a separate private "
                    "message to them. You may also act, answer proposals, or message others, but keep it focused.")
        else:
            task = ("It is your turn. Read your inbox and the world, answer any proposals, "
                    "and decide what to say and do this turn. "
                    "If something notable happened (see recent_events: a war, a city founded close to you or "
                    "changing hands, a wonder, a religion, a Congress vote, a big deal), you may speak to "
                    "everyone (to: \"all\") without being asked: brag, congratulate through gritted teeth, "
                    "condemn, warn, or rally others. Only when there's news worth reacting to, not every turn.")
            if "your_empire" in ctx:
                task += "\n" + ORDERS_TASK
                if review:
                    task += "\n" + REVIEW_TASK
        user = f"{CONTEXT_START}\n{json.dumps(ctx, separators=(',', ':'), ensure_ascii=False)}\n{CONTEXT_END}\n\n{task}"
        clients = [self.talk_client, self.client] if (with_human and self.talk_client) else [self.client]
        if self.talk_client and len(clients) == 1:
            clients.append(self.talk_client)   # a routine call that fails (or is refused) gets one more try
        retried = reshaped = False
        while clients:   # if one model stalls, fails or refuses, the other answers
            client = clients.pop(0)
            try:
                self.last_model = client.name
                raw = self._call(client, user, "talk" if with_human else mode, game.turn)
                decision = parse_decision(raw)
                if decision.error and not reshaped:
                    # Answered, but not in the JSON shape (now and then a plain-text reply): ask once more
                    reshaped = True
                    again = parse_decision(self._call(client, user + REPLY_AGAIN, "talk" if with_human else mode,
                                                      game.turn))
                    if not again.error or (again.raw or "").strip():
                        decision = again
                if decision.error and clients:
                    continue   # the other model gets its turn
                break
            except Exception as e:  # network errors etc. shouldn't kill the game
                decision = Decision(error=f"LLM call failed: {e}")
                if (with_human and client is self.talk_client and not retried
                        and isinstance(e, (subprocess.TimeoutExpired, TimeoutError))):
                    retried = True   # a stuck call: the same model again usually answers in seconds
                    clients.insert(0, client)
        self._remember(game.turn, decision)
        self.last_decision = decision
        return decision

    def consolidate(self, game: Game) -> bool:
        """Condense memory: the LLM rewrites what matters into short summaries, raw logs get trimmed.

        Keeps prompts small over a long game. Returns False (and changes nothing) if it fails.
        """
        ctx = self.build_context(game, "summarize", [])
        task = SUMMARIZE_TASK
        user = f"{CONTEXT_START}\n{json.dumps(ctx, indent=1)}\n{CONTEXT_END}\n\n{task}"
        try:
            raw = self._call(self.talk_client or self.client, user, "memory summary", game.turn)
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        except Exception:
            return False
        summaries = {}
        for item in data.get("relationships", []) if isinstance(data.get("relationships"), list) else []:
            try:
                summaries[int(item["about"])] = str(item.get("summary", ""))
            except (KeyError, TypeError, ValueError):
                continue
        self.memory.compress(summaries, str(data.get("story", "")))
        return True

    def _remember(self, turn: int, d: Decision) -> None:
        for m in d.memory:
            try:
                about = int(m.get("about"))
            except (TypeError, ValueError):
                continue
            if about == self.civ_id:
                continue
            try:
                delta = int(m.get("trust_change", 0) or 0)
            except (TypeError, ValueError):
                delta = 0
            self.memory.fresh += 1
            self.memory.rel(about).adjust(delta, str(m.get("note", ""))[:200],
                                          str(m.get("promise", "") or "")[:200], turn)
        self.memory.add_journal(turn, d.journal)
        steps = {c.get("id"): c for c in self.memory.strategy.get("checklist", [])} if self.memory.strategy else {}
        for u in d.checklist:
            step = steps.get(u.get("id"))
            if step and u.get("status") in ("done", "doing", "dropped"):
                step["status"] = u["status"]
                if u.get("note"):
                    note = re.sub(r"^\s*T\d+:\s*", "", str(u["note"]))   # the leader often stamps the turn itself
                    step["note"] = f"T{turn}: {note[:160]}"
