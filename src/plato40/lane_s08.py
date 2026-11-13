"""S08 guest desk lane on the bus (docs/BUS.md): every guest request gets one answer.

Dev has far more guest requests than any other lane answers. This lane answers each request
that no other lane cites, once: a ``notify_guest`` with the checked message of ``llm_guard``,
plus the staff action the request type asks for (an inspection, a repair order, a cleaning).
The request types S03 owns (``temperature_hot``, ``temperature_cold``) and the requests the
room turnaround planner already cites are left out, so no request has two answers.

- ``late_checkout``: offered pending staff approval if the room has no arrival the same day
  from bookings known at ``t``; otherwise luggage storage is offered instead. Never a promise.
- ``early_checkin``: "should be ready" only if the room is clean at ``t``; otherwise the
  guest is told a message follows. No time is quoted that the rules did not plan.
- Everything else gets a notice of what staff will do; no password, price or place is invented.

The message text never holds a booking ID. Every effect is simulated.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from plato40 import llm_guard, plan
from plato40.bus import VERSION
from plato40.detect import turnaround, water
from plato40.detect.grid import stamp
from plato40.episodes import group
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.state import RoomStates

LANE = "s08"
ISSUE = "guest_service"
Row = dict[str, Any]
OWNED = frozenset({"temperature_hot", "temperature_cold"})
CLOCK = re.compile(r"\b([01]?\d|2[0-3])[:.h]([0-5]\d)\b")

TEMPLATES: dict[str, dict[str, str]] = {
    "access": {
        "en": (
            "Sorry about the trouble getting into room {room}. We have asked our staff to "
            "help you with access."
        ),
        "de": (
            "Entschuldigen Sie die Probleme beim Zugang zu Zimmer {room}. Wir haben unser "
            "Personal gebeten, Ihnen beim Zutritt zu helfen."
        ),
        "fr": (
            "Nous sommes désolés pour la difficulté d'accès à la chambre {room}. Nous avons "
            "demandé à notre personnel de vous aider à entrer."
        ),
        "it": (
            "Ci scusiamo per il problema di accesso alla camera {room}. Abbiamo chiesto al "
            "nostro personale di aiutarla a entrare."
        ),
        "nl": (
            "Excuses voor het probleem met de toegang tot kamer {room}. We hebben ons "
            "personeel gevraagd u te helpen om binnen te komen."
        ),
        "es": (
            "Lamentamos el problema para acceder a la habitación {room}. Hemos pedido a "
            "nuestro personal que le ayude a entrar."
        ),
    },
    "maintenance": {
        "en": (
            "Thank you for reporting the fault in room {room}. We have passed it to our "
            "maintenance team, who will contact you before entering the room."
        ),
        "de": (
            "Danke für die Meldung des Defekts in Zimmer {room}. Wir haben ihn an unser "
            "Technikteam weitergegeben; es meldet sich bei Ihnen, bevor es das Zimmer "
            "betritt."
        ),
        "fr": (
            "Merci d'avoir signalé le problème dans la chambre {room}. Nous l'avons "
            "transmis à notre équipe technique, qui vous contactera avant d'entrer dans la "
            "chambre."
        ),
        "it": (
            "Grazie per la segnalazione del guasto nella camera {room}. L'abbiamo inoltrata "
            "al nostro team tecnico, che la contatterà prima di entrare in camera."
        ),
        "nl": (
            "Bedankt voor het melden van de storing in kamer {room}. We hebben dit "
            "doorgegeven aan ons technisch team, dat contact met u opneemt voordat het de "
            "kamer betreedt."
        ),
        "es": (
            "Gracias por avisar de la avería en la habitación {room}. La hemos pasado a "
            "nuestro equipo técnico, que le contactará antes de entrar en la habitación."
        ),
    },
    "water": {
        "en": (
            "Thank you for your message about the water in room {room}. We have asked our "
            "staff to check it and will contact you before entering the room."
        ),
        "de": (
            "Danke für Ihre Nachricht zum Wasser in Zimmer {room}. Wir haben unser Personal "
            "gebeten, es zu prüfen, und melden uns, bevor jemand das Zimmer betritt."
        ),
        "fr": (
            "Merci pour votre message concernant l'eau dans la chambre {room}. Nous avons "
            "demandé à notre personnel de vérifier et vous contacterons avant d'entrer dans "
            "la chambre."
        ),
        "it": (
            "Grazie per il messaggio sull'acqua nella camera {room}. Abbiamo chiesto al "
            "nostro personale di controllare e la contatteremo prima di entrare in camera."
        ),
        "nl": (
            "Bedankt voor uw bericht over het water in kamer {room}. We hebben ons "
            "personeel gevraagd dit te controleren en nemen contact op voordat iemand de "
            "kamer betreedt."
        ),
        "es": (
            "Gracias por su mensaje sobre el agua en la habitación {room}. Hemos pedido a "
            "nuestro personal que lo revise y le contactaremos antes de entrar en la "
            "habitación."
        ),
    },
    "air": {
        "en": (
            "Thank you for your message about the air in room {room}. We have asked our "
            "staff to check it and will contact you before entering the room."
        ),
        "de": (
            "Danke für Ihre Nachricht zur Luft in Zimmer {room}. Wir haben unser Personal "
            "gebeten, sie zu prüfen, und melden uns, bevor jemand das Zimmer betritt."
        ),
        "fr": (
            "Merci pour votre message concernant l'air de la chambre {room}. Nous avons "
            "demandé à notre personnel de vérifier et vous contacterons avant d'entrer dans "
            "la chambre."
        ),
        "it": (
            "Grazie per il messaggio sull'aria nella camera {room}. Abbiamo chiesto al "
            "nostro personale di controllare e la contatteremo prima di entrare in camera."
        ),
        "nl": (
            "Bedankt voor uw bericht over de lucht in kamer {room}. We hebben ons personeel "
            "gevraagd dit te controleren en nemen contact op voordat iemand de kamer "
            "betreedt."
        ),
        "es": (
            "Gracias por su mensaje sobre el aire de la habitación {room}. Hemos pedido a "
            "nuestro personal que lo revise y le contactaremos antes de entrar en la "
            "habitación."
        ),
    },
    "cleaning": {
        "en": (
            "Thank you for your request for room {room}. We have passed it to housekeeping, "
            "who will contact you before entering the room."
        ),
        "de": (
            "Danke für Ihre Anfrage zu Zimmer {room}. Wir haben sie an das Housekeeping "
            "weitergegeben; es meldet sich bei Ihnen, bevor es das Zimmer betritt."
        ),
        "fr": (
            "Merci pour votre demande concernant la chambre {room}. Nous l'avons transmise "
            "au service d'étage, qui vous contactera avant d'entrer dans la chambre."
        ),
        "it": (
            "Grazie per la richiesta per la camera {room}. L'abbiamo inoltrata al servizio "
            "di pulizia, che la contatterà prima di entrare in camera."
        ),
        "nl": (
            "Bedankt voor uw verzoek voor kamer {room}. We hebben het doorgegeven aan "
            "housekeeping, dat contact met u opneemt voordat het de kamer betreedt."
        ),
        "es": (
            "Gracias por su solicitud para la habitación {room}. La hemos pasado a "
            "housekeeping, que le contactará antes de entrar en la habitación."
        ),
    },
    "late_free": {
        "en": (
            "Thank you for your request for room {room}. A later check-out until {time} "
            "looks possible; our staff will decide and let you know."
        ),
        "de": (
            "Danke für Ihre Anfrage zu Zimmer {room}. Ein späterer Check-out bis {time} "
            "scheint möglich; unser Personal entscheidet und meldet sich bei Ihnen."
        ),
        "fr": (
            "Merci pour votre demande concernant la chambre {room}. Un départ tardif "
            "jusqu'à {time} semble possible ; notre personnel décidera et vous répondra."
        ),
        "it": (
            "Grazie per la richiesta per la camera {room}. Un check-out posticipato fino "
            "alle {time} sembra possibile; il nostro personale deciderà e la informerà."
        ),
        "nl": (
            "Bedankt voor uw verzoek voor kamer {room}. Een latere check-out tot {time} "
            "lijkt mogelijk; ons personeel beslist en laat het u weten."
        ),
        "es": (
            "Gracias por su solicitud para la habitación {room}. Salir más tarde, hasta las "
            "{time}, parece posible; nuestro personal decidirá y le avisará."
        ),
    },
    "late_any": {
        "en": (
            "Thank you for your request for room {room}. A later check-out looks possible; "
            "our staff will decide and let you know."
        ),
        "de": (
            "Danke für Ihre Anfrage zu Zimmer {room}. Ein späterer Check-out scheint "
            "möglich; unser Personal entscheidet und meldet sich bei Ihnen."
        ),
        "fr": (
            "Merci pour votre demande concernant la chambre {room}. Un départ tardif semble "
            "possible ; notre personnel décidera et vous répondra."
        ),
        "it": (
            "Grazie per la richiesta per la camera {room}. Un check-out posticipato sembra "
            "possibile; il nostro personale deciderà e la informerà."
        ),
        "nl": (
            "Bedankt voor uw verzoek voor kamer {room}. Een latere check-out lijkt "
            "mogelijk; ons personeel beslist en laat het u weten."
        ),
        "es": (
            "Gracias por su solicitud para la habitación {room}. Salir más tarde parece "
            "posible; nuestro personal decidirá y le avisará."
        ),
    },
    "late_busy": {
        "en": (
            "Thank you for your request for room {room}. The room is needed for the next "
            "guest, so a later check-out is not possible. We can offer luggage storage "
            "after check-out."
        ),
        "de": (
            "Danke für Ihre Anfrage zu Zimmer {room}. Das Zimmer wird für den nächsten Gast "
            "benötigt, ein späterer Check-out ist daher nicht möglich. Nach dem Check-out "
            "können wir Ihr Gepäck aufbewahren."
        ),
        "fr": (
            "Merci pour votre demande concernant la chambre {room}. La chambre est "
            "nécessaire pour le client suivant, un départ tardif n'est donc pas possible. "
            "Nous pouvons garder vos bagages après le départ."
        ),
        "it": (
            "Grazie per la richiesta per la camera {room}. La camera serve al prossimo "
            "ospite, quindi un check-out posticipato non è possibile. Dopo il check-out "
            "possiamo custodire i bagagli."
        ),
        "nl": (
            "Bedankt voor uw verzoek voor kamer {room}. De kamer is nodig voor de volgende "
            "gast, dus een latere check-out is niet mogelijk. Na het uitchecken kunnen we "
            "uw bagage bewaren."
        ),
        "es": (
            "Gracias por su solicitud para la habitación {room}. La habitación se necesita "
            "para el siguiente huésped, por lo que no es posible salir más tarde. Tras el "
            "check-out podemos guardar su equipaje."
        ),
    },
    "early_ready": {
        "en": (
            "Thank you for your request for room {room}. The room should be ready for you; "
            "our staff will message you if anything changes."
        ),
        "de": (
            "Danke für Ihre Anfrage zu Zimmer {room}. Das Zimmer sollte voraussichtlich "
            "bereit sein; unser Personal meldet sich, falls sich etwas ändert."
        ),
        "fr": (
            "Merci pour votre demande concernant la chambre {room}. La chambre devrait être "
            "prête ; notre personnel vous écrira si quelque chose change."
        ),
        "it": (
            "Grazie per la richiesta per la camera {room}. La camera dovrebbe essere "
            "pronta; il nostro personale la avviserà se qualcosa cambia."
        ),
        "nl": (
            "Bedankt voor uw verzoek voor kamer {room}. De kamer is naar verwachting klaar; "
            "ons personeel laat het u weten als er iets verandert."
        ),
        "es": (
            "Gracias por su solicitud para la habitación {room}. La habitación debería "
            "estar lista; nuestro personal le avisará si algo cambia."
        ),
    },
    "early_wait": {
        "en": (
            "Thank you for your request for room {room}. The room is not ready yet; we will "
            "message you when it is ready."
        ),
        "de": (
            "Danke für Ihre Anfrage zu Zimmer {room}. Das Zimmer ist noch nicht bereit; wir "
            "schreiben Ihnen, sobald es fertig ist."
        ),
        "fr": (
            "Merci pour votre demande concernant la chambre {room}. La chambre n'est pas "
            "encore prête ; nous vous écrirons dès qu'elle le sera."
        ),
        "it": (
            "Grazie per la richiesta per la camera {room}. La camera non è ancora pronta; "
            "le scriveremo quando sarà pronta."
        ),
        "nl": (
            "Bedankt voor uw verzoek voor kamer {room}. De kamer is nog niet klaar; we "
            "sturen u een bericht zodra die klaar is."
        ),
        "es": (
            "Gracias por su solicitud para la habitación {room}. La habitación aún no está "
            "lista; le escribiremos cuando lo esté."
        ),
    },
    "waiting": {
        "en": (
            "Sorry for the wait for room {room}. Our staff are checking it now and will "
            "message you when it is ready."
        ),
        "de": (
            "Entschuldigen Sie die Wartezeit auf Zimmer {room}. Unser Personal prüft es "
            "jetzt und schreibt Ihnen, sobald es fertig ist."
        ),
        "fr": (
            "Nous nous excusons pour l'attente de la chambre {room}. Notre personnel "
            "vérifie maintenant et vous écrira dès qu'elle sera prête."
        ),
        "it": (
            "Ci scusiamo per l'attesa della camera {room}. Il nostro personale sta "
            "controllando e le scriverà quando sarà pronta."
        ),
        "nl": (
            "Excuses voor het wachten op kamer {room}. Ons personeel controleert het nu en "
            "stuurt u een bericht zodra die klaar is."
        ),
        "es": (
            "Disculpe la espera de la habitación {room}. Nuestro personal la está revisando "
            "y le escribirá cuando esté lista."
        ),
    },
    "wifi": {
        "en": (
            "Thank you for your message about the WiFi in room {room}. Our staff will look "
            "into it and get back to you."
        ),
        "de": (
            "Danke für Ihre Nachricht zum WLAN in Zimmer {room}. Unser Personal kümmert "
            "sich darum und meldet sich bei Ihnen."
        ),
        "fr": (
            "Merci pour votre message concernant le Wi-Fi de la chambre {room}. Notre "
            "personnel s'en occupe et reviendra vers vous."
        ),
        "it": (
            "Grazie per il messaggio sul Wi-Fi della camera {room}. Il nostro personale se "
            "ne occuperà e la ricontatterà."
        ),
        "nl": (
            "Bedankt voor uw bericht over de wifi in kamer {room}. Ons personeel zoekt het "
            "uit en komt bij u terug."
        ),
        "es": (
            "Gracias por su mensaje sobre el wifi de la habitación {room}. Nuestro personal "
            "lo revisará y le responderá."
        ),
    },
    "parking": {
        "en": (
            "Thank you for your question about parking for room {room}. Our reception will "
            "get back to you with the details."
        ),
        "de": (
            "Danke für Ihre Frage zum Parken für Zimmer {room}. Unsere Rezeption meldet "
            "sich mit den Einzelheiten bei Ihnen."
        ),
        "fr": (
            "Merci pour votre question sur le stationnement pour la chambre {room}. Notre "
            "réception reviendra vers vous avec les détails."
        ),
        "it": (
            "Grazie per la domanda sul parcheggio per la camera {room}. La nostra reception "
            "la ricontatterà con i dettagli."
        ),
        "nl": (
            "Bedankt voor uw vraag over parkeren voor kamer {room}. Onze receptie komt bij "
            "u terug met de details."
        ),
        "es": (
            "Gracias por su pregunta sobre el aparcamiento para la habitación {room}. "
            "Nuestra recepción le responderá con los detalles."
        ),
    },
    "invoice": {
        "en": (
            "Thank you for your invoice request for room {room}. Our reception will prepare "
            "it and get back to you."
        ),
        "de": (
            "Danke für Ihre Rechnungsanfrage zu Zimmer {room}. Unsere Rezeption bereitet "
            "sie vor und meldet sich bei Ihnen."
        ),
        "fr": (
            "Merci pour votre demande de facture pour la chambre {room}. Notre réception la "
            "préparera et reviendra vers vous."
        ),
        "it": (
            "Grazie per la richiesta di fattura per la camera {room}. La nostra reception "
            "la preparerà e la ricontatterà."
        ),
        "nl": (
            "Bedankt voor uw factuurverzoek voor kamer {room}. Onze receptie maakt die "
            "klaar en komt bij u terug."
        ),
        "es": (
            "Gracias por su solicitud de factura para la habitación {room}. Nuestra "
            "recepción la preparará y le responderá."
        ),
    },
    "luggage": {
        "en": (
            "Thank you for your question about luggage storage for room {room}. Our "
            "reception will tell you how it works."
        ),
        "de": (
            "Danke für Ihre Frage zur Gepäckaufbewahrung für Zimmer {room}. Unsere "
            "Rezeption sagt Ihnen, wie es funktioniert."
        ),
        "fr": (
            "Merci pour votre question sur la consigne à bagages pour la chambre {room}. "
            "Notre réception vous expliquera comment cela fonctionne."
        ),
        "it": (
            "Grazie per la domanda sul deposito bagagli per la camera {room}. La nostra "
            "reception le spiegherà come funziona."
        ),
        "nl": (
            "Bedankt voor uw vraag over bagageopslag voor kamer {room}. Onze receptie legt "
            "u uit hoe het werkt."
        ),
        "es": (
            "Gracias por su pregunta sobre la consigna de equipaje para la habitación "
            "{room}. Nuestra recepción le explicará cómo funciona."
        ),
    },
    "towels": {
        "en": (
            "Thank you for your request for room {room}. We have asked housekeeping to "
            "bring fresh towels."
        ),
        "de": (
            "Danke für Ihre Anfrage zu Zimmer {room}. Wir haben das Housekeeping gebeten, "
            "frische Handtücher zu bringen."
        ),
        "fr": (
            "Merci pour votre demande concernant la chambre {room}. Nous avons demandé au "
            "service d'étage d'apporter des serviettes propres."
        ),
        "it": (
            "Grazie per la richiesta per la camera {room}. Abbiamo chiesto al servizio di "
            "pulizia di portare asciugamani puliti."
        ),
        "nl": (
            "Bedankt voor uw verzoek voor kamer {room}. We hebben housekeeping gevraagd "
            "verse handdoeken te brengen."
        ),
        "es": (
            "Gracias por su solicitud para la habitación {room}. Hemos pedido a "
            "housekeeping que lleve toallas limpias."
        ),
    },
    "noise": {
        "en": (
            "We are sorry about the noise near room {room}. Our staff have been informed "
            "and will look into it."
        ),
        "de": (
            "Es tut uns leid wegen des Lärms bei Zimmer {room}. Unser Personal ist "
            "informiert und kümmert sich darum."
        ),
        "fr": (
            "Nous sommes désolés pour le bruit près de la chambre {room}. Notre personnel "
            "est informé et s'en occupe."
        ),
        "it": (
            "Ci dispiace per il rumore vicino alla camera {room}. Il nostro personale è "
            "stato informato e se ne occuperà."
        ),
        "nl": (
            "Het spijt ons van het lawaai bij kamer {room}. Ons personeel is op de hoogte "
            "en zoekt het uit."
        ),
        "es": (
            "Lamentamos el ruido cerca de la habitación {room}. Nuestro personal está "
            "informado y lo revisará."
        ),
    },
    "delay": {
        "en": (
            "Thank you for letting us know about your late arrival to room {room}. We have "
            "noted it and informed our reception."
        ),
        "de": (
            "Danke für die Nachricht zu Ihrer späten Ankunft in Zimmer {room}. Wir haben es "
            "notiert und unsere Rezeption informiert."
        ),
        "fr": (
            "Merci de nous avoir prévenus de votre arrivée tardive pour la chambre {room}. "
            "Nous l'avons notée et en avons informé notre réception."
        ),
        "it": (
            "Grazie per averci avvisato del suo arrivo in ritardo per la camera {room}. Lo "
            "abbiamo annotato e informato la nostra reception."
        ),
        "nl": (
            "Bedankt dat u ons op de hoogte brengt van uw late aankomst voor kamer {room}. "
            "We hebben het genoteerd en onze receptie geïnformeerd."
        ),
        "es": (
            "Gracias por avisarnos de su llegada tardía a la habitación {room}. Lo hemos "
            "anotado e informado a nuestra recepción."
        ),
    },
}


# request_type -> (template, intent of the staff action or None, action, what staff does)
ASKS: dict[str, tuple[str, str | None, str | None, str]] = {
    "access_issue": (
        "access",
        "inspect_room",
        "request_inspection",
        "Send staff to the room: the guest cannot get in",
    ),
    "maintenance_issue": (
        "maintenance",
        "order_repair",
        "order_service",
        "Order a repair of the fault the guest reported",
    ),
    "water_issue": (
        "water",
        "inspect_room",
        "request_inspection",
        "Inspect the water in the room: the guest reports a running toilet or leak",
    ),
    "air_quality": (
        "air",
        "inspect_room",
        "request_inspection",
        "Inspect the air in the room: the guest reports stale or bad air",
    ),
    "cleaning_request": (
        "cleaning",
        "clean_room",
        "schedule_cleaning",
        "Clean the room as the guest asked",
    ),
    "wifi_help": ("wifi", None, None, "Staff look into the WiFi and reply"),
    "parking": ("parking", None, None, "Reception replies with the parking details"),
    "invoice": ("invoice", None, None, "Reception prepares the invoice and replies"),
    "luggage_storage": ("luggage", None, None, "Reception explains luggage storage"),
    "extra_towels": ("towels", None, None, "Housekeeping brings fresh towels"),
    "noise_complaint": ("noise", None, None, "Staff look into the noise"),
    "arrival_delay": ("delay", None, None, "Reception notes the late arrival"),
    "waiting_for_room": ("waiting", None, None, "Staff check the room and message the guest"),
}


@dataclass
class _Board:
    """The rows this lane writes, with per-episode sequence numbers for their IDs."""

    rows: list[Row] = field(default_factory=list)
    seq: Counter[str] = field(default_factory=Counter)

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
            "facts": dict(facts),
            "cause": "guest_request",
        }
        self.rows.append(row)
        return row

    def propose(
        self,
        eid: str,
        t: pd.Timestamp,
        sig: Row,
        intent: str,
        action: str,
        priority: str,
        reason: str,
        **extra: Any,
    ) -> None:
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
            "assets": list(sig["assets"]),
            "priority": priority,
            "evidence": list(sig["evidence"]),
            "reason": reason,
            "cause": sig["cause"],
        }
        row.update({k: v for k, v in extra.items() if v is not None})
        self.rows.append(row)


@dataclass
class _Item:
    """One request to answer: its row, the variant of its message and the phrasing task."""

    req: Mapping[str, Any]
    t: pd.Timestamp
    variant: str
    task: llm_guard.Task
    says: str


def _planner_cited(ds: Dataset, settings: Settings) -> set[str]:
    """Return the requests the room turnaround planner cites in its decisions."""
    leaks = water.signals(ds, settings)
    signals = turnaround.signals(ds, settings, water.out_of_order(leaks, ds, settings))
    if "s05" in settings.lanes:
        signals = [s for s in signals if s.kind != "day_plan"]
    episodes = group(signals, settings.detect.episode_quiet_h)
    decisions = plan.decisions(episodes, RoomStates(ds), settings)
    return {g for d in decisions for g in d.get("guest_request_ids", [])}


def _arrival_same_day(ds: Dataset, req: Mapping[str, Any], t: pd.Timestamp, tz: str) -> bool:
    """Return whether the room has another arrival on the guest's departure day, known at ``t``."""
    bk = ds.table("bookings.csv")
    own = bk.loc[bk["booking_id"] == req["booking_id"]]
    if own.empty:
        return False
    depart = pd.Timestamp(own["departure_utc"].iloc[0])
    day = depart.tz_convert(tz).date()
    later = bk.loc[
        (bk["room_id"] == req["room_id"])
        & (bk["status"] == "confirmed")
        & (bk["created_utc"] <= t)
        & (bk["arrival_utc"] >= depart)
    ]
    return bool((later["arrival_utc"].dt.tz_convert(tz).dt.date == day).any())


def _variant(
    ds: Dataset, states: RoomStates, tz: str, req: Mapping[str, Any], t: pd.Timestamp
) -> tuple[str, dict[str, str], bool, str]:
    """Return the template, its say map, whether it is hedged, and what the rules said."""
    room, kind = str(req["room_id"]), str(req["request_type"])
    say = {"room": room}
    if kind == "late_checkout":
        if _arrival_same_day(ds, req, t, tz):
            return "late_busy", say, False, "a later check-out is not possible, luggage storage"
        found = CLOCK.search(str(req["text"]))
        if found is None:
            return "late_any", say, False, "a later check-out looks possible, pending staff"
        say["time"] = f"{int(found[1]):02d}:{found[2]}"
        return "late_free", say, False, f"a later check-out until {say['time']} looks possible"
    if kind == "early_checkin":
        if states.at(room, t) == "clean":
            return "early_ready", say, True, "the room should be ready, expected"
        return "early_wait", say, False, "the room is not ready, a message follows"
    return ASKS[kind][0], say, False, ASKS[kind][3]


def rows(
    ds: Dataset, settings: Settings, cache: Path, call: llm_guard.Call | None = None
) -> tuple[list[Row], Counter[str]]:
    """Return the s08 signal and proposal rows in time order, and the messages by source."""
    tz = settings.local_tz
    states = RoomStates(ds)
    req = ds.table("guest_requests.csv").sort_values(["timestamp_utc", "request_id"], kind="stable")
    cited = _planner_cited(ds, settings)
    items: list[_Item] = []
    for rec in req.to_dict("records"):
        r: dict[str, Any] = {str(k): v for k, v in rec.items()}
        kind = str(r["request_type"])
        if kind in OWNED or r["request_id"] in cited:
            continue
        t = pd.Timestamp(r["timestamp_utc"]).ceil("min")
        if t >= pd.Timestamp(r["response_due_utc"]):
            continue
        variant, say, hedged, says = _variant(ds, states, tz, r, t)
        task = llm_guard.Task(
            lane=LANE,
            t=stamp(t),
            known_by=stamp(t),
            language=str(r["language"]),
            intent=f"guest_{variant}",
            facts={"room": r["room_id"], "request_type": kind},
            say=say,
            must_say=tuple(say),
            template=TEMPLATES[variant][str(r["language"])],
            hedged=hedged,
        )
        items.append(_Item(r, t, variant, task, says))
    done = llm_guard.prefetch([i.task for i in items], cache, call)
    board = _Board()
    sources: Counter[str] = Counter()
    for it in items:
        q, t = it.req, it.t
        rid, room, kind = str(q["request_id"]), str(q["room_id"]), str(q["request_type"])
        prio = str(q["priority"])
        due = stamp(pd.Timestamp(q["response_due_utc"]))
        eid = f"EP-{ISSUE}-{room}-{t:%Y%m%dT%H%M}Z"
        sig = board.signal(
            eid,
            t,
            kind,
            [room],
            [f"guest_requests.csv:{rid}"],
            f"Guest request ({kind}) from {room}, answer due {due}",
            {"request_type": kind, "language": q["language"], "variant": it.variant},
        )
        said = llm_guard.phrase(it.task, cache, call, done=done)
        sources[said.source] += 1
        ids = [rid]
        extra = ASKS.get(kind)
        reason = f"Answer the guest request from {room}: {it.says}"
        if "time" in it.task.say:
            reason += f" at {it.task.say['time']}"
        board.propose(
            eid,
            t,
            sig,
            "answer_request",
            "notify_guest",
            prio,
            reason + (" expected" if it.task.hedged else "") + ".",
            message={"language": q["language"], "text": said.text},
            guest_request_ids=ids,
            confidence=0.7,
            expected_outcome="The guest knows what staff will do (simulated).",
            verification_plan="No repeat of the same request from the booking before it is due.",
        )
        if extra is not None and extra[1] is not None and extra[2] is not None:
            board.propose(
                eid,
                t,
                sig,
                extra[1],
                extra[2],
                prio,
                f"{extra[3]} in {room}.",
                guest_request_ids=ids,
                confidence=0.7,
                expected_outcome="The room is handled by staff (simulated).",
                verification_plan="A staff visit or work order for the room before the due time.",
            )
    order = {"signal": 0, "proposal": 1}
    board.rows.sort(key=lambda x: (x.get("t_known") or x["t"], order[x["row"]], x["id"]))
    return board.rows, sources
