"""S03 trigger lane on the bus (docs/BUS.md): heating and comfort signals and proposals.

Lane ``s03``, issue ``heating_comfort``. Every row is timed when its inputs were known
(ADR-002), and every proposal answers a signal of its own episode.

Signals (``kind``, ``cause``) and what they propose:

- ``cop_low`` (HP1, ``hp_degradation``): each hourly check whose 24 h COP ratio is low
  (``detect.heating``). Checks with no gap above ``episode_quiet_h`` form one HP1 episode.
  The first check proposes the flag, the service (the in-house technician with a claim, or
  a contractor when none is on shift within ``technician_horizon_h``) and the comfort-first
  energy plan. The first check at or after ``contractor_after_h`` past the technician's
  start, with the COP still low, proposes a contractor.
- ``comfort_deficit``:
  - at a ``cop_low`` check, guests whose occupied room was in comfort deficit in the last
    hour and who were not told yet in this episode (``hp_degradation``): one
    ``notify_guest`` per guest;
  - at a guest's ``temperature_cold`` request: a reply to that guest at the request time,
    so before its ``response_due_utc``. While HP1 is degraded the reply joins the HP1
    episode (``hp_degradation``). Otherwise the room gets its own episode (``unknown``, or
    ``sensor_gap`` with no valid reading in the last hour): a setpoint boost to the top of
    the band if the room is below the band and its window is not reported open, and a room
    heating flag if it is also in deficit below its setpoint. With the window reported
    open, the reply asks the guest to close it instead.
- ``cold_forecast`` (``cold_snap``): at each forecast issue while HP1 is degraded, if the
  coldest hour of the next 24 h is below ``preheat_forecast_c``: a pre-heat plan.
- ``price_peak``: at each tariff publication while HP1 is degraded: shift load off the
  dearest published hours, only where occupied rooms stay in the band.

HP1 is degraded at ``t`` when the hourly check at ``t`` floored to the hour is ``cop_low``.
Guest messages go through ``llm_guard``: a cached, checked answer, else the template. The
language is the request's, else the booking's latest request known at ``t``, else English.
Proposals never carry ``autonomy`` or approval flags; the arbiter's policy sets them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from plato40 import llm_guard
from plato40.bus import LANES, LANGUAGES, VERSION
from plato40.contract import PRIORITIES
from plato40.detect import heating
from plato40.detect.grid import local_time, stamp
from plato40.episodes import Signal, group
from plato40.load.derive import comfort_deficit, stays, telemetry_wide
from plato40.load.reader import Dataset
from plato40.load.units import BOUNDS
from plato40.settings import Settings
from plato40.staff import Roster

LANE = "s03"
ISSUE = str(LANES[LANE]["issue_type"])
Row = dict[str, Any]

# Guest message templates: the only token is {room}; the rules render it (llm_guard.Task.say).
TEMPLATES: dict[str, dict[str, str]] = {
    "hp": {
        "en": "Dear guest in room {room}: our heating is running at reduced capacity and a "
        "repair has been requested. We are directing heat to occupied rooms first. We "
        "apologise for the inconvenience.",
        "de": "Liebe Gäste in Zimmer {room}: Unsere Heizung läuft derzeit mit verringerter "
        "Leistung, eine Reparatur ist angefordert. Wir leiten die Wärme vorrangig in belegte "
        "Zimmer. Wir entschuldigen uns für die Unannehmlichkeiten.",
        "fr": "Chers hôtes de la chambre {room} : notre chauffage fonctionne actuellement à "
        "puissance réduite et une réparation a été demandée. Nous dirigeons la chaleur en "
        "priorité vers les chambres occupées. Veuillez nous excuser pour ce désagrément.",
        "it": "Gentili ospiti della camera {room}: il nostro riscaldamento funziona al momento a "
        "potenza ridotta ed è stata richiesta una riparazione. Diamo la priorità al "
        "riscaldamento delle camere occupate. Ci scusiamo per il disagio.",
        "nl": "Beste gasten in kamer {room}: onze verwarming werkt momenteel op verminderd "
        "vermogen en er is een reparatie aangevraagd. We geven bezette kamers voorrang bij de "
        "verwarming. Excuses voor het ongemak.",
        "es": "Estimados huéspedes de la habitación {room}: nuestra calefacción funciona ahora "
        "con potencia reducida y se ha solicitado una reparación. Damos prioridad a la "
        "calefacción de las habitaciones ocupadas. Disculpe las molestias.",
    },
    "room": {
        "en": "Thank you for your message about the temperature in room {room}. We are "
        "checking the heating now and will contact you if a visit to the room is needed.",
        "de": "Danke für Ihre Nachricht zur Temperatur in Zimmer {room}. Wir prüfen die "
        "Heizung jetzt und melden uns, falls ein Besuch im Zimmer nötig ist.",
        "fr": "Merci pour votre message concernant la température de la chambre {room}. Nous "
        "vérifions le chauffage maintenant et vous contacterons si une visite dans la chambre "
        "est nécessaire.",
        "it": "Grazie per il messaggio sulla temperatura della camera {room}. Stiamo "
        "controllando ora il riscaldamento e la contatteremo se sarà necessario un intervento "
        "in camera.",
        "nl": "Bedankt voor uw bericht over de temperatuur in kamer {room}. We controleren de "
        "verwarming nu en nemen contact met u op als een bezoek aan de kamer nodig is.",
        "es": "Gracias por su mensaje sobre la temperatura de la habitación {room}. Estamos "
        "revisando la calefacción y le contactaremos si es necesario entrar en la habitación.",
    },
    "window": {
        "en": "Thank you for your message about room {room}. The window sensor shows an open "
        "window; please close it so the room can warm up. We are also checking the heating.",
        "de": "Danke für Ihre Nachricht zu Zimmer {room}. Der Fenstersensor meldet ein offenes "
        "Fenster; bitte schließen Sie es, damit das Zimmer warm werden kann. Wir prüfen "
        "zusätzlich die Heizung.",
        "fr": "Merci pour votre message concernant la chambre {room}. Le capteur indique une "
        "fenêtre ouverte ; veuillez la fermer pour que la chambre puisse se réchauffer. Nous "
        "vérifions aussi le chauffage.",
        "it": "Grazie per il messaggio sulla camera {room}. Il sensore segnala una finestra "
        "aperta; la preghiamo di chiuderla perché la camera possa riscaldarsi. Controlliamo "
        "anche il riscaldamento.",
        "nl": "Bedankt voor uw bericht over kamer {room}. De sensor meldt een open raam; sluit "
        "het alstublieft zodat de kamer kan opwarmen. We controleren ook de verwarming.",
        "es": "Gracias por su mensaje sobre la habitación {room}. El sensor indica una ventana "
        "abierta; por favor, ciérrela para que la habitación pueda calentarse. También "
        "revisamos la calefacción.",
    },
}
SAYS = {
    "hp": "heating runs at reduced capacity, a repair is requested and heat goes to occupied "
    "rooms first",
    "room": "the heating of the room is being checked now",
    "window": "the window sensor shows an open window; please close it; the heating is checked",
}


def _json(x: Any) -> Any:
    """Return ``x`` as JSON: UTC times as ``...Z``, numpy scalars as Python, floats rounded."""
    if x is pd.NaT or (isinstance(x, float) and np.isnan(x)):
        return None
    if isinstance(x, pd.Timestamp):
        return stamp(x)
    if isinstance(x, Mapping):
        return {str(k): _json(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_json(v) for v in x]
    if isinstance(x, np.generic):
        return _json(x.item())
    if isinstance(x, float):
        return round(x, 4)
    return x


@dataclass
class _Board:
    """The rows this lane writes, with per-episode sequence numbers for their IDs."""

    rows: list[Row] = field(default_factory=list)
    seq: Counter[str] = field(default_factory=Counter)
    messages: Counter[str] = field(default_factory=Counter)

    def _id(self, eid: str, what: str) -> str:
        self.seq[f"{eid}/{what}"] += 1
        return f"{LANE}-{eid.removeprefix('EP-')}-{what}{self.seq[f'{eid}/{what}']:02d}"

    def signal(
        self,
        eid: str,
        t: pd.Timestamp,
        kind: str,
        assets: Iterable[str],
        evidence: Iterable[str],
        detail: str,
        facts: Mapping[str, Any],
        cause: str | None = None,
    ) -> Row:
        row: Row = {
            "v": VERSION,
            "row": "signal",
            "lane": LANE,
            "id": self._id(eid, "sig"),
            "t_known": stamp(t),
            "issue_type": ISSUE,
            "kind": kind,
            "assets": list(assets),
            "evidence": list(evidence),
            "detail": detail,
            "facts": _json(facts),
        }
        if cause is not None:
            row["cause"] = cause
        self.rows.append(row)
        return row

    def propose(
        self,
        eid: str,
        t: pd.Timestamp,
        sig: Row,
        intent: str,
        action: str,
        assets: Iterable[str],
        priority: str,
        reason: str,
        **extra: Any,
    ) -> Row:
        row: Row = {
            "v": VERSION,
            "row": "proposal",
            "lane": LANE,
            "id": self._id(eid, "prop"),
            "t": stamp(t),
            "source": "rules",
            "episode_id": eid,
            "signal_ids": [sig["id"]],
            "intent": intent,
            "action": action,
            "assets": list(assets),
            "priority": priority,
            "evidence": list(sig["evidence"]),
            "reason": reason,
        }
        if "cause" in sig:
            row["cause"] = sig["cause"]
        row.update({k: v for k, v in extra.items() if v is not None})
        self.rows.append(row)
        return row


@dataclass
class _Hp:
    """What was already proposed in one HP1 episode."""

    told: set[str] = field(default_factory=set)
    deadline: pd.Timestamp | None = None


class _Lane:
    """Builds the s03 rows of one dataset in time order."""

    def __init__(
        self, ds: Dataset, settings: Settings, cache: Path, call: llm_guard.Call | None
    ) -> None:
        self.ds, self.settings, self.cache, self.call = ds, settings, cache, call
        self.th = settings.detect
        lo, hi = ds.building["constraints"]["occupied_comfort_c"]
        self.lo, self.hi = float(lo), float(hi)
        self.board = _Board()
        self.roster = Roster(ds)
        self.stays = stays(ds)
        self.requests = ds.table("guest_requests.csv")
        bound = BOUNDS[("room_telemetry.csv", "temperature_c")]
        temp = telemetry_wide(ds, "temperature_c")
        self.temp = temp.where((temp >= bound.low) & (temp <= bound.high))
        self.recent = pd.Timedelta(hours=self.th.room_reading_h)
        self.outlook = pd.Timedelta(hours=self.th.cold_outlook_h)
        self.deficit = comfort_deficit(ds, self.lo, self.th.comfort_deficit_k, self.temp)
        self.setpoint = telemetry_wide(ds, "setpoint_c")
        self.window = telemetry_wide(ds, "window_open")
        cop = heating.signals(ds, settings)
        self.cop: dict[pd.Timestamp, tuple[str, Signal]] = {}
        self.first: set[pd.Timestamp] = set()
        for ep in group(cop, self.th.episode_quiet_h):
            self.first.add(ep.signals[0].time)
            for s in ep.signals:
                self.cop[s.time] = (ep.episode_id, s)
        self.hp: dict[str, _Hp] = {}

    # ---------------------------------------------------------------- facts known at t

    def _degraded(self, t: pd.Timestamp) -> tuple[str, Signal] | None:
        """Return the HP1 episode and check whose hourly verdict at ``t`` is ``cop_low``."""
        return self.cop.get(t.floor("h"))

    def _booking(self, room: str, at: pd.Timestamp) -> str | None:
        st = self.stays
        hit = st.loc[(st["room_id"] == room) & (st["start"] <= at) & (st["end"] > at)]
        return str(hit["booking_id"].iloc[0]) if len(hit) else None

    def _language(self, booking: str, t: pd.Timestamp) -> str:
        r = self.requests
        mine = r.loc[(r["booking_id"] == booking) & (r["timestamp_utc"] <= t)]
        earlier = mine.sort_values("timestamp_utc", kind="stable")["language"]
        lang = str(earlier.iloc[-1]) if len(earlier) else "en"
        return lang if lang in LANGUAGES else "en"

    def _last(self, frame: pd.DataFrame, room: str, t: pd.Timestamp) -> float | None:
        if room not in frame.columns:
            return None
        col = frame[room]
        recent = col.loc[(col.index > t - self.recent) & (col.index <= t)].dropna()
        return float(recent.iloc[-1]) if len(recent) else None

    def _local(self, t: pd.Timestamp, ref: pd.Timestamp) -> str:
        return local_time(t, ref, self.settings.local_tz)

    # ---------------------------------------------------------------- guest message

    def _notify(
        self,
        eid: str,
        t: pd.Timestamp,
        sig: Row,
        room: str,
        booking: str,
        language: str,
        template: str,
        priority: str,
        request: str | None = None,
    ) -> None:
        task = llm_guard.Task(
            lane=LANE,
            t=stamp(t),
            known_by=sig["t_known"],
            language=language,
            intent="notify_guest_comfort",
            facts={"room": room, "booking": booking, "cause": sig.get("cause", "unknown")},
            say={"room": room},
            must_say=("room",),
            template=TEMPLATES[template][language],
        )
        said = llm_guard.phrase(task, self.cache, self.call)
        self.board.messages[said.source] += 1
        what = SAYS[template]
        self.board.propose(
            eid,
            t,
            sig,
            "notify_guest_comfort",
            "notify_guest",
            [room],
            priority,
            f"Tell the guest of {booking} in {room} ({language}): {what} (proposed message; "
            "delivery not confirmed).",
            message={"language": language, "text": said.text},
            guest_request_ids=[request] if request else None,
            confidence=0.8 if request else 0.7,
            expected_outcome="The guest knows the cause and that help is coming.",
            verification_plan=(
                f"No further temperature_cold request from {booking}; escalate on one."
            ),
        )

    # ---------------------------------------------------------------- cop_low

    def _cop(self, t: pd.Timestamp) -> None:
        eid, s = self.cop[t]
        d = s.data
        th = self.th
        state = self.hp.setdefault(eid, _Hp())
        facts = {k: d[k] for k in ("ratio", "outdoor", "band", "cold", "peaks", "cold_rooms")}
        facts |= {"technician": d["technician"], "technician_from": d["technician_from"]}
        sig = self.board.signal(
            eid, t, "cop_low", ("HP1",), s.evidence, s.detail, facts, "hp_degradation"
        )
        if t in self.first:
            self._open(eid, t, sig, d, state)
        elif state.deadline is not None and t >= state.deadline:
            state.deadline = None
            self.board.propose(
                eid,
                t,
                sig,
                "escalate_contractor",
                "order_service",
                ["HP1"],
                "high",
                f"Service HP1 by a contractor: COP still {d['ratio']:.2f} of reference "
                f"{th.contractor_after_h:.0f} h after the technician's start.",
                confidence=0.7,
                expected_outcome="Contractor fixes the fault (simulated).",
                verification_plan="24 h COP ratio recovers after the visit.",
            )
        self._cold_guests(eid, t, state)

    def _open(self, eid: str, t: pd.Timestamp, sig: Row, d: Mapping[str, Any], state: _Hp) -> None:
        th = self.th
        self.board.propose(
            eid,
            t,
            sig,
            "flag_hp_degradation",
            "flag_anomaly",
            ["HP1"],
            "high",
            f"Heat pump degradation: {sig['detail']}.",
            confidence=0.8,
            expected_outcome="Operations see the degraded heat pump; no physical effect.",
            verification_plan="24 h COP back above the reference ratio after service.",
        )
        when: pd.Timestamp | None = d["technician_from"]
        horizon = pd.Timedelta(hours=th.technician_horizon_h)
        on = [] if when is None else self.roster.on_shift("technician", when, t)
        if when is None or when - t > horizon or not on:
            self.board.propose(
                eid,
                t,
                sig,
                "escalate_contractor",
                "order_service",
                ["HP1"],
                "high",
                f"Service HP1 by a contractor: no technician on shift within "
                f"{th.technician_horizon_h:.0f} h.",
                confidence=0.7,
                expected_outcome="Contractor fixes the fault (simulated).",
                verification_plan="24 h COP ratio recovers after the visit.",
            )
        else:
            who = on[0]
            start = "now" if when <= t else "from " + self._local(when, t)
            self.board.propose(
                eid,
                t,
                sig,
                "service_heat_pump",
                "dispatch_technician",
                ["HP1"],
                "high",
                f"Service HP1 (COP {d['ratio']:.2f} of reference); technician {who} on shift "
                f"{start}.",
                resource_id=who,
                claims=[
                    {
                        "res": "technician",
                        "id": who,
                        "from": stamp(when),
                        "min": int(th.technician_job_h * 60),
                    }
                ],
                confidence=0.8,
                expected_outcome="Technician finds and fixes the fault (simulated).",
                verification_plan=(
                    f"COP ratio back above {th.cop_ratio_low:.2f} within "
                    f"{th.contractor_after_h:.0f} h of the technician's start; a contractor is "
                    "ordered if not."
                ),
            )
            state.deadline = when + pd.Timedelta(hours=th.contractor_after_h)
        cold = (
            f"coldest forecast hour {d['cold'][0]:.1f} degC at {self._local(d['cold'][1], t)}"
            if d["cold"]
            else "no forecast known"
        )
        peaks = ", ".join(d["peaks"]) or "unknown"
        self.board.propose(
            eid,
            t,
            sig,
            "protect_occupied_rooms",
            "optimize_energy",
            ["HP1", "BUILDING"],
            "high",
            f"Protect occupied rooms with reduced heat: hold vacant rooms at setback, pre-heat "
            f"occupied rooms within {self.lo:.0f}-{self.hi:.0f} degC before the {cold}; avoid "
            f"price peaks ({peaks}) only where comfort holds. Comfort before cost.",
            confidence=0.6,
            expected_outcome="Occupied rooms stay in the comfort band (simulated).",
            verification_plan=(
                f"Occupied rooms at or above {self.lo:.0f} degC; escalate rooms that fall below."
            ),
        )

    def _cold_guests(self, eid: str, t: pd.Timestamp, state: _Hp) -> None:
        """Signal guests newly in comfort deficit in the last hour and tell each of them."""
        idx = self.deficit.index
        recent = self.deficit.loc[(idx > t - self.recent) & (idx <= t)]
        guests: list[tuple[str, str, str, pd.Timestamp]] = []
        for room in sorted(str(c) for c in recent.columns[recent.any(axis=0).to_numpy()]):
            when = pd.Timestamp(recent.index[recent[room].to_numpy(dtype=bool)][-1])
            booking = self._booking(room, when)
            if booking is None or booking in state.told or self._booking(room, t) != booking:
                continue
            guests.append((room, booking, self._language(booking, t), when))
        if not guests:
            return
        rooms = [g[0] for g in guests]
        sig = self.board.signal(
            eid,
            t,
            "comfort_deficit",
            rooms,
            [
                f"room_telemetry.csv:{','.join(rooms)} temperature_c,setpoint_c,window_open:"
                f"{stamp(t - self.recent)}..{stamp(t)}",
                *(f"bookings.csv:{b}" for _, b, _, _ in guests),
            ],
            f"{len(guests)} occupied rooms newly below {self.lo:.0f} degC and at least "
            f"{self.th.comfort_deficit_k:.1f} K below setpoint while HP1 is degraded",
            {
                "band": [self.lo, self.hi],
                "guests": [
                    {"room": r, "booking": b, "language": lang, "seen": w}
                    for r, b, lang, w in guests
                ],
            },
            "hp_degradation",
        )
        for room, booking, lang, _ in guests:
            self._notify(eid, t, sig, room, booking, lang, "hp", "high")
            state.told.add(booking)

    # ---------------------------------------------------------------- forecast and tariff

    def _forecast(self, t: pd.Timestamp) -> None:
        hit = self._degraded(t)
        if hit is None:
            return
        eid, s = hit
        fc = self.ds.table("weather_forecast.csv")
        ahead = fc.loc[
            (fc["issued_utc"] == t)
            & (fc["target_utc"] > t)
            & (fc["target_utc"] <= t + self.outlook)
        ].dropna(subset=["outdoor_c_forecast"])
        if ahead.empty:
            return
        row = ahead.iloc[int(ahead["outdoor_c_forecast"].to_numpy(dtype=float).argmin())]
        low, at = float(row["outdoor_c_forecast"]), pd.Timestamp(row["target_utc"])
        if low >= self.th.preheat_forecast_c:
            return
        sig = self.board.signal(
            eid,
            t,
            "cold_forecast",
            ("HP1", "BUILDING"),
            (f"weather_forecast.csv:issued {stamp(t)}", *s.evidence),
            f"forecast issued {stamp(t)}: coldest hour {low:.1f} degC at {stamp(at)} while "
            f"HP1 runs at {s.data['ratio']:.2f} of its reference COP",
            {"cold": [low, at], "ratio": s.data["ratio"], "band": [self.lo, self.hi]},
            "cold_snap",
        )
        self.board.propose(
            eid,
            t,
            sig,
            "preheat_before_cold",
            "optimize_energy",
            ["HP1", "BUILDING"],
            "high",
            f"Pre-heat occupied rooms towards {self.hi:.0f} degC (top of the band) before the "
            f"coldest forecast hour {low:.1f} degC at {self._local(at, t)}, while HP1 runs "
            "degraded; vacant rooms stay at setback unless a guest arrives before then.",
            confidence=0.6,
            expected_outcome="Occupied rooms enter the cold hours warm (simulated).",
            verification_plan=(
                f"Occupied rooms at or above {self.lo:.0f} degC through {self._local(at, t)}; "
                "escalate rooms that fall below."
            ),
        )

    def _tariff(self, t: pd.Timestamp) -> None:
        hit = self._degraded(t)
        if hit is None:
            return
        eid, s = hit
        tariff = self.ds.table("tariff.csv")
        day = tariff.loc[tariff["published_utc"] == t].dropna(subset=["price_eur_kwh"])
        if day.empty:
            return
        top = day.nlargest(self.th.price_peaks, "price_eur_kwh").sort_values("timestamp_utc")
        hours = [pd.Timestamp(x) for x in top["timestamp_utc"]]
        named = ", ".join(self._local(h, t) for h in hours)
        sig = self.board.signal(
            eid,
            t,
            "price_peak",
            ("HP1", "BUILDING"),
            (f"tariff.csv:published {stamp(t)}", *s.evidence),
            f"tariff published {stamp(t)}: dearest hours {named} while HP1 is degraded",
            {
                "peaks": hours,
                "prices": [float(p) for p in top["price_eur_kwh"]],
                "band": [self.lo, self.hi],
            },
        )
        self.board.propose(
            eid,
            t,
            sig,
            "shift_load_off_peak",
            "optimize_energy",
            ["HP1", "BUILDING"],
            "medium",
            f"Shift HP1 load off the dearest published hours ({named}) only where occupied "
            f"rooms stay at or above {self.lo:.0f} degC: pre-heat before them; occupied rooms "
            "in deficit keep full heat. Comfort before cost.",
            confidence=0.5,
            expected_outcome="Less HP1 load in the dearest hours, comfort held (simulated).",
            verification_plan=(
                f"No occupied room below {self.lo:.0f} degC in the shifted hours; otherwise "
                "heat at any price."
            ),
        )

    # ---------------------------------------------------------------- guest requests

    def _request(self, t: pd.Timestamp, r: pd.Series, room_eid: str | None) -> None:
        rid, room, booking = str(r["request_id"]), str(r["room_id"]), str(r["booking_id"])
        lang = str(r["language"]) if str(r["language"]) in LANGUAGES else "en"
        priority = str(r["priority"]) if str(r["priority"]) in PRIORITIES else "medium"
        temp = self._last(self.temp, room, t)
        setpoint = self._last(self.setpoint, room, t)
        window = self._last(self.window, room, t)
        evidence = [
            f"guest_requests.csv:{rid}",
            f"room_telemetry.csv:{room} temperature_c,setpoint_c,window_open:"
            f"{stamp(t - self.recent)}..{stamp(t)}",
        ]
        measured = "no valid reading in the last hour" if temp is None else f"{temp:.1f} degC"
        opened = window == 1.0
        if opened:
            measured += ", window open"
        facts = {
            "request": rid,
            "booking": booking,
            "language": lang,
            "due": r["response_due_utc"],
            "temperature": temp,
            "setpoint": setpoint,
            "window_open": window,
            "band": [self.lo, self.hi],
        }
        hit = self._degraded(t)
        if hit is not None:
            eid, _ = hit
            sig = self.board.signal(
                eid,
                t,
                "comfort_deficit",
                (room,),
                evidence,
                f"guest of {booking} reports cold in {room} ({measured}) while HP1 is degraded",
                facts,
                "hp_degradation",
            )
            template = "window" if opened else "hp"
            self._notify(eid, t, sig, room, booking, lang, template, priority, rid)
            self.hp.setdefault(eid, _Hp()).told.add(booking)
            return
        if room_eid is None:
            raise RuntimeError(f"{rid}: no room episode for a request outside HP1 episodes")
        sig = self.board.signal(
            room_eid,
            t,
            "comfort_deficit",
            (room,),
            evidence,
            f"guest of {booking} reports cold in {room} ({measured}); HP1 not degraded at the "
            "last hourly check",
            facts,
            "sensor_gap" if temp is None else "unknown",
        )
        if temp is not None and temp < self.lo and not opened:
            if setpoint is None or setpoint < self.hi:
                self.board.propose(
                    room_eid,
                    t,
                    sig,
                    "boost_room_setpoint",
                    "adjust_room_setpoint",
                    [room],
                    priority,
                    f"Raise the setpoint of {room} to {self.hi:.0f} degC (top of the band): "
                    f"{temp:.1f} degC is below {self.lo:.0f} degC and the guest is cold.",
                    confidence=0.6,
                    expected_outcome=f"{room} reaches {self.lo:.0f} degC (simulated).",
                    verification_plan=(
                        f"{room} at or above {self.lo:.0f} degC within "
                        f"{self.th.room_heat_verify_h:.0f} h; otherwise inspect "
                        "the room's heating (approval, guest consent)."
                    ),
                )
            if setpoint is not None and setpoint - temp >= self.th.comfort_deficit_k:
                self.board.propose(
                    room_eid,
                    t,
                    sig,
                    "flag_room_heating",
                    "flag_anomaly",
                    [room],
                    priority,
                    f"{room} is {temp:.1f} degC against a {setpoint:.1f} degC setpoint, the "
                    "window not reported open and HP1 not degraded at the last hourly check: "
                    "suspect the room's heating.",
                    confidence=0.6,
                    expected_outcome="Operations see a room heating fault; no physical effect.",
                    verification_plan=(
                        f"{room} at or above {self.lo:.0f} degC within "
                        f"{self.th.room_heat_verify_h:.0f} h; otherwise inspect "
                        "the room's heating (approval, guest consent)."
                    ),
                )
        template = "window" if opened else "room"
        self._notify(room_eid, t, sig, room, booking, lang, template, priority, rid)

    # ---------------------------------------------------------------- run

    def run(self) -> list[Row]:
        cold = self.requests.loc[self.requests["request_type"] == "temperature_cold"]
        cold = cold.assign(t=cold["timestamp_utc"].dt.ceil("min")).sort_values(["t", "request_id"])
        outside = [
            Signal(pd.Timestamp(t), ISSUE, "comfort_deficit", (str(room),), (), "")
            for t, room in zip(cold["t"], cold["room_id"], strict=True)
            if self._degraded(pd.Timestamp(t)) is None
        ]
        room_eid: dict[tuple[pd.Timestamp, str], str] = {}
        for ep in group(outside, self.th.episode_quiet_h):
            for s in ep.signals:
                room_eid[(s.time, s.assets[0])] = ep.episode_id
        steps: list[tuple[pd.Timestamp, int, str, Any]] = [(t, 0, "", None) for t in self.cop]
        fc = self.ds.table("weather_forecast.csv")["issued_utc"].dropna().unique()
        steps += [(pd.Timestamp(t), 1, "", None) for t in fc]
        pub = self.ds.table("tariff.csv")["published_utc"].dropna().unique()
        steps += [(pd.Timestamp(t), 2, "", None) for t in pub]
        steps += [(pd.Timestamp(r["t"]), 3, str(r["request_id"]), r) for _, r in cold.iterrows()]
        for t, kind, _, r in sorted(steps, key=lambda x: x[:3]):
            if kind == 0:
                self._cop(t)
            elif kind == 1:
                self._forecast(t)
            elif kind == 2:
                self._tariff(t)
            else:
                self._request(t, r, room_eid.get((t, str(r["room_id"]))))
        return self.board.rows


def rows(
    ds: Dataset, settings: Settings, cache: Path, call: llm_guard.Call | None = None
) -> tuple[list[Row], Counter[str]]:
    """Return the s03 signal and proposal rows of ``ds`` in time order, and message sources.

    ``cache`` is the LLM answer cache (``llm_guard``); with no cached answer and no ``call``
    every guest message is its template.
    """
    lane = _Lane(ds, settings, cache, call)
    out = lane.run()
    return out, lane.board.messages
