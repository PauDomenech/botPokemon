# -*- coding: utf-8 -*-
"""
Bot de Pokémon Showdown para DOBLES aleatorias (Random Doubles estilo VGC)
--------------------------------------------------------------------------
- Evita auto-daño: nunca pega al aliado y penaliza AOE peligrosos.
- Arregla crash de `move.target`: ahora se normaliza (enum/objeto → str).
- Más "tipo-consciente": puntúa por STAB, efectividad e INMUNIDADES.
- Menos cambios tontos: en fallback elige un ataque seguro antes que random.
- Logs de decisión: muestra top candidatos y objetivos.
"""

from __future__ import annotations
import asyncio
from typing import Dict, List, Optional, Tuple

from poke_env import AccountConfiguration, ShowdownServerConfiguration
from poke_env.player import Player
from poke_env.data import GenData
from poke_env.player.battle_order import DoubleBattleOrder, BattleOrder

# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# CONFIGURACIÓN
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

USER = "PaXBotVGC"   # <- cambia a tu alt si PS te devuelve "!usuario"
PASS = "123456"            # si la cuenta NO está registrada, deja "" (vacío)
FORMAT = "gen9randomdoublesbattle"  # dobles aleatorio (existe en PS)
VERBOSE = True
DEBUG_DECISIONS = True  # pon False para silenciar el log

SPREAD_DAMAGE_MOD = 0.75  # en dobles, los spreads hacen 0.75× daño


def dbg(*a):
    if DEBUG_DECISIONS:
        try:
            print("[DBG]", *a)
        except Exception:
            pass

EARLY_TURNS = 3

# Conjuntos de ids en minúsculas
SETUP_IDS = {"swordsdance", "nastyplot", "calmmind", "quiverdance", "bulkup"}
PROTECT_IDS = {"protect", "detect", "spikyshield", "kingsshield", "banefulbunker", "silktrap"}
SPEED_CTRL = {"trickroom", "tailwind", "icywind", "electroweb"}
REDIRECT_IDS = {"followme", "ragepowder"}
PIVOT_IDS = {"uturn", "voltswitch", "flipturn", "partingshot", "teleport"}
WIDE_GUARD_IDS = {"wideguard"}

# Spreads frecuentes (para no pedir target):
SPREAD_HINTS = {
    "earthquake", "bulldoze", "rockslide", "dazzlinggleam", "heatwave",
    "snarl", "muddywater", "eruption", "blizzard", "discharge", "hypervoice",
    "sludgewave", "water_spout", "makeitrain", "glaciallance", "originpulse",
    "precipiceblades", "astralbarrage"
}

# AOE que puede dañar al aliado (a vigilar)
AOE_FRIENDLY_FIRE = {
    "earthquake", "bulldoze", "eruption", "water_spout", "surf", "discharge",
    "sludgewave", "explosion", "selfdestruct"
}

# Singles dañinos que podrían usarse mal sobre el aliado con target "any/adjacentAlly" (en randoms, mejor evitar)
HARMFUL_ALLY_SINGLES = {"beatup"}


class VGCHeuristicsRandom(Player):
    """Jugador heurístico para DOBLES aleatorias con trazas y adaptación en tiempo real."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._typedata = None
        self.series_memory: Dict[str, Dict[str, bool]] = {}
        self._prefer_target_slot: Optional[int] = None  # 1/2 para doble focus

    # ---------- utilidades de tipos / normalización ----------
    def _ensure_type_chart(self):
        if self._typedata is None:
            self._typedata = GenData.from_format(self.format).type_chart

    def _eff(self, atk_type, defender) -> float:
        self._ensure_type_chart()
        if not atk_type or not defender:
            return 1.0
        return atk_type.damage_multiplier(*defender.types, type_chart=self._typedata)

    def _stab(self, move, attacker) -> float:
        return 1.5 if move.type and attacker and move.type in attacker.types else 1.0

    def _acc(self, move) -> float:
        return move.accuracy or 1.0

    @staticmethod
    def _target_str(move) -> str:
        """Normaliza move.target a str en minúsculas (maneja enum/objeto)."""
        t = getattr(move, "target", None)
        try:
            if isinstance(t, str):
                return t.lower()
            name = getattr(t, "name", None)
            if name is not None:
                return str(name).lower()
            value = getattr(t, "value", None)
            if value is not None:
                return str(value).lower()
            return str(t).lower() if t is not None else ""
        except Exception:
            return ""

    def _is_spread(self, move) -> bool:
        t = self._target_str(move)
        mid = (getattr(move, "id", "") or "").lower()
        return (mid in SPREAD_HINTS) or (t in {"alladjacentfoes", "alladjacent", "all", "foes"})

    def _requires_explicit_target(self, move) -> bool:
        """True si PS exige elegir 1/2; False para spreads/self/side/randomnormal."""
        if self._is_spread(move):
            return False
        t = self._target_str(move)
        if t in {"self", "ally", "allyteam", "allyside", "foeside", "allies", "randomnormal"}:
            return False
        # En dobles, 'any' o 'adjacentally/adjacentfoe' requieren 1/2 para pegar al rival correcto.
        return t in {"normal", "adjacentfoe", "adjacentally", "any"}

    @staticmethod
    def _is_physical(move) -> bool:
        return str(getattr(move, "category", "")).lower() == "physical"

    @staticmethod
    def _is_special(move) -> bool:
        return str(getattr(move, "category", "")).lower() == "special"

    @staticmethod
    def _stage_mod(stage: int) -> float:
        return (2 + max(stage, 0)) / (2 if stage >= 0 else (2 - stage))

    def _atk_mult(self, me, move) -> float:
        mult = 1.0
        if self._is_physical(move):
            mult *= self._stage_mod(int(getattr(me, "boosts", {}).get("atk", 0)))
            if getattr(me, "status", None) == "brn":
                mult *= 0.65
        elif self._is_special(move):
            mult *= self._stage_mod(int(getattr(me, "boosts", {}).get("spa", 0)))
        return mult

    # ---------- helpers de dobles (slots) ----------
    def _slot_index(self, battle, me) -> int:
        try:
            for i, p in enumerate((battle.active_pokemon or [])[:2]):
                if p is me:
                    return i
        except Exception:
            pass
        return 0

    def _moves_for_slot(self, battle, slot: int) -> List:
        # poke-env puede exponer available_moves como [lista, lista] o split por slot
        mv = getattr(battle, "available_moves", [])
        if mv and isinstance(mv[0], list):
            return mv[slot] if slot < len(mv) else []
        alt = getattr(battle, "available_moves2", None)
        if alt is not None:
            return mv if slot == 0 else alt
        return mv

    # ---------- memoria simple de sets rivales (Protect/speed) ----------
    def _get_series_mem(self, battle) -> Dict[str, bool]:
        opp = getattr(battle, "opponent_username", None) or "_unknown_"
        if opp not in self.series_memory:
            self.series_memory[opp] = {"protect": False, "speed_ctrl": False}
        return self.series_memory[opp]

    def _update_series_mem_from_battle(self, battle):
        mem = self._get_series_mem(battle)
        for mon in (battle.opponent_team or {}).values():
            if not mon or not mon.moves:
                continue
            ids = {(m.id or "").lower() for m in mon.moves.values()}
            if ids & PROTECT_IDS:
                mem["protect"] = True
            if ids & SPEED_CTRL:
                mem["speed_ctrl"] = True

    # ---------- ajustes de score ----------
    def _protect_risk_factor(self, target) -> float:
        try:
            last = getattr(target, "last_move", None)
            if last and (getattr(last, "id", "").lower() in PROTECT_IDS):
                return 0.6
        except Exception:
            pass
        return 1.0

    def _move_score_vs_single(self, move, me, target, mem: Dict[str, bool]) -> float:
        if not move:
            return 0.0
        mid = (move.id or "").lower()

        # Evita pegar al aliado si el move permite "any/adjacentally" y es dañino.
        t = self._target_str(move)
        if (t in {"adjacentally", "ally", "any"}) and (move.base_power or 0) > 0:
            if mid in HARMFUL_ALLY_SINGLES:
                return 0.0

        # Estado útil (pero preferimos atacar en randoms)
        if not move.base_power or move.base_power == 0:
            if any(k in mid for k in SETUP_IDS):
                need = 0
                if "swordsdance" in mid or "bulkup" in mid:
                    need = -int(getattr(me, "boosts", {}).get("atk", 0))
                elif any(k in mid for k in ("nastyplot", "calmmind", "quiverdance")):
                    need = -int(getattr(me, "boosts", {}).get("spa", 0))
                return 26.0 + 1.5 * max(0, need)
            if any(k in mid for k in SPEED_CTRL):
                spe_penalty = int(getattr(me, "boosts", {}).get("spe", 0))
                par_pen = 1 if getattr(me, "status", None) == "par" else 0
                return 32.0 + 3.0 * max(0, -spe_penalty + par_pen)
            if mid in REDIRECT_IDS:
                return 28.0
            if mid in PROTECT_IDS:
                return 28.0 + (3.0 if mem.get("protect") else 0.0)
            if mid in WIDE_GUARD_IDS:
                return 27.0
            if mid in PIVOT_IDS:
                atk_drop = int(getattr(me, "boosts", {}).get("atk", 0))
                spa_drop = int(getattr(me, "boosts", {}).get("spa", 0))
                drop_bonus = 3.0 if (atk_drop <= -2 or spa_drop <= -2) else 0.0
                return 12.0 + drop_bonus
            return 4.0

        # Ataques: potencia efectiva por tipos/boosts/accuracy
        eff = self._eff(move.type, target)
        if eff == 0:
            return 0.0
        score = (move.base_power) * self._stab(move, me) * eff * self._acc(move) * self._atk_mult(me, move)
        # Potencia un poco más si es súper eficaz x2 o x4
        if eff >= 4:
            score *= 1.20
        elif eff >= 2:
            score *= 1.12
        # Remate
        if target and target.current_hp_fraction is not None and target.current_hp_fraction < 0.35:
            score *= 1.15
        # Riesgos
        if getattr(move, "recharge", False) or "hyperbeam" in mid:
            score *= 0.7
        score *= self._protect_risk_factor(target)
        return float(score)

    def _move_score_spread(self, move, me, opps, mem: Dict[str, bool], ally=None) -> float:
        if not move:
            return 0.0
        mid = (move.id or "").lower()
        total = 0.0
        live_opps = [o for o in opps if o and not o.fainted]
        for t in live_opps:
            eff = self._eff(move.type, t)
            if eff == 0:
                continue
            part = move.base_power * self._stab(move, me) * eff * self._acc(move) * self._atk_mult(me, move)
            if eff >= 4:
                part *= 1.12
            elif eff >= 2:
                part *= 1.06
            if t.current_hp_fraction is not None and t.current_hp_fraction < 0.35:
                part *= 1.10
            part *= self._protect_risk_factor(t)
            total += part
        # Fuego amigo: penaliza salvo que el aliado sea inmune/beneficiado
        if mid in AOE_FRIENDLY_FIRE and ally is not None and len(live_opps) >= 1:
            safe = self._ally_safe_for_aoe(mid, move.type, ally)
            if not safe:
                total *= 0.20
        total = total * SPREAD_DAMAGE_MOD * 1.03
        return float(total)

    # ---------- inmunidades del aliado a AOE ----------
    def _ally_safe_for_aoe(self, move_id: str, move_type, ally) -> bool:
        if ally is None:
            return True
        ability = (ally.ability or "").lower() if hasattr(ally, "ability") else ""
        if ability == "telepathy":
            return True
        mid = (move_id or "").lower()
        types = {getattr(t, "name", str(t)) for t in (ally.types or [])}
        item = (ally.item or "").lower() if hasattr(ally, "item") else ""
        if mid in {"earthquake", "bulldoze"}:
            return ("Flying" in types) or (ability in {"levitate"}) or (item == "airballoon")
        if mid in {"explosion", "selfdestruct"}:
            return ("Ghost" in types)
        if mid == "discharge":
            return ("Ground" in types) or (ability in {"voltabsorb", "lightningrod"})
        if mid in {"surf", "water_spout"}:
            return ability in {"waterabsorb", "dryskin"}
        if mid in {"heatwave", "eruption"}:
            return ability in {"flashfire"}
        if mid == "sludgewave":
            return False
        return False

    # ---------- elección mejor movimiento y objetivo ----------
    def _best_move_and_target(self, battle, me, opps, mem: Dict[str, bool], ally=None, prefer_slot: Optional[int]=None) -> Tuple[float, Optional[object], int, str]:
        """(score, move, target_index_ps, reason). target_index_ps: 1/2; 0 para auto/spread."""
        slot = self._slot_index(battle, me)
        my_moves = self._moves_for_slot(battle, slot)
        if not me or not battle or not my_moves:
            return (0.0, None, 0, "no-moves")
        live_opps = [o for o in opps if o and not o.fainted]
        if not live_opps:
            return (0.0, None, 0, "no-opps")

        candidates: List[Tuple[float, object, int, str]] = []
        # Spread
        for m in my_moves:
            if self._is_spread(m):
                s = self._move_score_spread(m, me, opps, mem, ally=ally)
                if s > 0:
                    candidates.append((s, m, 0, "spread"))
        # Single-target
        tmap = [(live_opps[0], 1)] + ([(live_opps[1], 2)] if len(live_opps) > 1 else [])
        for m in my_moves:
            if not self._is_spread(m):
                for tgt, ps in tmap:
                    s = self._move_score_vs_single(m, me, tgt, mem)
                    if prefer_slot and ps == prefer_slot:
                        s *= 1.20  # más foco
                    if s > 0:
                        candidates.append((s, m, ps, "single"))
        if not candidates:
            return (0.0, None, 0, "no-cands")
        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0]
        try:
            top3 = [(getattr(m, 'id', '?'), round(s,1), ('t'+str(ps) if ps else 'auto'), why) for s,m,ps,why in candidates[:3]]
            dbg(f"T{getattr(battle,'turn',1)} slot={slot} me={getattr(me,'species','?')} hp={getattr(me,'current_hp_fraction',1):.2f}")
            dbg("  top:", top3)
        except Exception:
            pass
        return best

    # ---------- decisión de movimientos por turno ----------
    def choose_move(self, battle):
        try:
            mem = self._get_series_mem(battle)
            self._prefer_target_slot = None

            me_list: List = [p for p in (getattr(battle, "active_pokemon", []) or []) if p]
            opp_list: List = [p for p in (getattr(battle, "opponent_active_pokemon", []) or []) if p]
            if len(me_list) == 0:
                return self.choose_random_doubles_move(battle)

            orders: List[Optional[BattleOrder]] = [None, None]

            # Slot 0
            if len(me_list) > 0:
                me0 = me_list[0]
                ally1 = me_list[1] if len(me_list) > 1 else None
                s0, mv0, tgt0, why0 = self._best_move_and_target(battle, me0, opp_list, mem, ally=ally1)
                if mv0:
                    if self._requires_explicit_target(mv0) and tgt0 in (1, 2):
                        orders[0] = self.create_order(mv0, move_target=tgt0)
                        self._prefer_target_slot = tgt0
                        dbg(f"T{battle.turn} slot=0 -> MOVE {getattr(mv0,'id','?')} tgt={tgt0} [{why0}]")
                    else:
                        orders[0] = self.create_order(mv0)
                        dbg(f"T{battle.turn} slot=0 -> MOVE {getattr(mv0,'id','?')} (no target) [{why0}]")
                else:
                    slot_moves0 = self._moves_for_slot(battle, 0)
                    mv = next((m for m in slot_moves0 if (m.base_power or 0) > 0 and not self._is_spread(m)), None)
                    if not mv:
                        mv = next((m for m in slot_moves0 if (m.base_power or 0) > 0), None)
                    if mv:
                        orders[0] = self.create_order(mv)
                        dbg(f"T{battle.turn} slot=0 -> SIMPLE ATTACK (fallback)")
                    else:
                        dbg(f"T{battle.turn} slot=0 -> RANDOM (no better option)")
                        return self.choose_random_doubles_move(battle)

            # Slot 1
            if len(me_list) > 1:
                me1 = me_list[1]
                ally0 = me_list[0]
                s1, mv1, tgt1, why1 = self._best_move_and_target(battle, me1, opp_list, mem, ally=ally0, prefer_slot=self._prefer_target_slot)
                if mv1:
                    if self._requires_explicit_target(mv1) and tgt1 in (1, 2):
                        orders[1] = self.create_order(mv1, move_target=tgt1)
                        dbg(f"T{battle.turn} slot=1 -> MOVE {getattr(mv1,'id','?')} tgt={tgt1} [{why1}]")
                    else:
                        orders[1] = self.create_order(mv1)
                        dbg(f"T{battle.turn} slot=1 -> MOVE {getattr(mv1,'id','?')} (no target) [{why1}]")
                else:
                    slot_moves1 = self._moves_for_slot(battle, 1)
                    mv = next((m for m in slot_moves1 if (m.base_power or 0) > 0 and not self._is_spread(m)), None)
                    if not mv:
                        mv = next((m for m in slot_moves1 if (m.base_power or 0) > 0), None)
                    if mv:
                        orders[1] = self.create_order(mv)
                        dbg(f"T{battle.turn} slot=1 -> SIMPLE ATTACK (fallback)")
                    else:
                        dbg(f"T{battle.turn} slot=1 -> RANDOM (no better option)")
                        return self.choose_random_doubles_move(battle)

            # Asegura dos órdenes
            for i in range(2):
                if orders[i] is None:
                    dbg(f"T{battle.turn} WARN: missing order for slot {i}, using random")
                    return self.choose_random_doubles_move(battle)

            dbg(f"T{battle.turn} -> DoubleOrder ready")
            return DoubleBattleOrder(orders[0], orders[1])

        except Exception as e:
            if VERBOSE:
                print(f"[CHOOSE_MOVE ERROR] {e}")
            return self.choose_random_doubles_move(battle)

    # ---------- hooks ----------
    def _battle_finished_callback(self, battle):
        try:
            self._update_series_mem_from_battle(battle)
            dbg("Battle finished.")
        except Exception:
            pass


async def main():
    account_cfg = AccountConfiguration(USER, PASS)
    server_cfg = ShowdownServerConfiguration  # PS oficial

    bot = VGCHeuristicsRandom(
        account_configuration=account_cfg,
        server_configuration=server_cfg,
        battle_format=FORMAT,
        save_replays="replays_vgc_random",
        log_level=25,
        accept_open_team_sheet=True,
    )

    print(f"[INFO] Conectando como {USER} en formato {FORMAT} (dobles aleatorio)...")
    print("[INFO] Para desafiar al bot desde tu cuenta principal en PS! usa:")
    print(f"[INFO]   /challenge {USER}, {FORMAT}")

    async def _login_check():
        await asyncio.sleep(3)
        if bot.username and str(bot.username).startswith("!"):
            msg = (
                "[WARN] Showdown devolvió un nombre con '!' (guest forzado)."
                "       Sugerencias: usa un USER diferente o registra la cuenta"
                "       en PS y pon PASS correcto; si no está registrada, deja PASS=\"\"."
            )
            print(msg)

    asyncio.create_task(_login_check())

    try:
        await bot.accept_challenges(None, 1_000_000)
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("[INFO] Cancelado por el usuario. Cerrando...")
    finally:
        print("[INFO] Bot detenido.")


if __name__ == "__main__":
    asyncio.run(main())
