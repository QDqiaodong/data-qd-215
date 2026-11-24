from __future__ import annotations
from fractions import Fraction


def _num(value: object, name: str) -> Fraction:
    if not isinstance(value,str) or not value or len(value)>32: raise ValueError(f"{name} must be a decimal string")
    try: return Fraction(value)
    except (ValueError,ZeroDivisionError) as exc: raise ValueError(f"invalid {name}") from exc


def _text(x: Fraction) -> str:
    return str(x.numerator) if x.denominator==1 else f"{x.numerator}/{x.denominator}"


def process(data: dict) -> dict:
    samples=data.get("samples") if isinstance(data,dict) else None
    threshold=_num(data.get("active_current_a"),"active_current_a")
    min_duration=_num(data.get("min_phase_seconds"),"min_phase_seconds")
    if threshold<=0 or min_duration<0: raise ValueError("threshold must be positive and duration nonnegative")
    if not isinstance(samples,list) or not 2<=len(samples)<=100000: raise ValueError("samples must contain 2..100000 items")
    parsed=[]; ids=set(); previous=None
    for sample in samples:
        sid=sample.get("id") if isinstance(sample,dict) else None
        if not isinstance(sid,str) or not sid or sid in ids: raise ValueError("sample ids must be unique")
        time=_num(sample.get("time_s"),"time_s"); voltage=_num(sample.get("voltage_v"),"voltage_v"); current=_num(sample.get("current_a"),"current_a")
        if time<0 or voltage<=0: raise ValueError("time and voltage out of range")
        if previous is not None and time<=previous: raise ValueError("sample time must strictly increase")
        previous=time; ids.add(sid); parsed.append((sid,time,voltage,current))

    def state(current: Fraction) -> str:
        if current>=threshold: return "charge"
        if current<=-threshold: return "discharge"
        return "rest"

    phases=[]; current_phase=None
    for left,right in zip(parsed,parsed[1:]):
        sid,t0,v0,i0=left; _,t1,v1,i1=right
        phase_state=state(i0); dt=t1-t0
        avg_i=(i0+i1)/2; avg_v=(v0+v1)/2
        amp_hours=avg_i*dt/3600
        watt_hours=avg_i*avg_v*dt/3600
        if current_phase is None or current_phase["state"]!=phase_state:
            current_phase={"state":phase_state,"start_sample_id":sid,"start_time_s":t0,"end_time_s":t1,"amp_hours":amp_hours,"watt_hours":watt_hours}
            phases.append(current_phase)
        else:
            current_phase["end_time_s"]=t1; current_phase["amp_hours"]+=amp_hours; current_phase["watt_hours"]+=watt_hours
    output=[]
    for index,phase in enumerate(phases,1):
        duration=phase["end_time_s"]-phase["start_time_s"]
        output.append({"phase_id":f"p{index}","state":phase["state"],"start_sample_id":phase["start_sample_id"],"start_time_s":_text(phase["start_time_s"]),"end_time_s":_text(phase["end_time_s"]),"duration_s":_text(duration),"amp_hours":_text(phase["amp_hours"]),"watt_hours":_text(phase["watt_hours"]),"status":"accepted" if phase["state"]=="rest" or duration>=min_duration else "too_short"})
    charge=sum((p["watt_hours"] for p in phases if p["state"]=="charge"),Fraction(0))
    discharge=-sum((p["watt_hours"] for p in phases if p["state"]=="discharge"),Fraction(0))
    efficiency=None if charge==0 else discharge/charge
    return {"phases":output,"charge_wh":_text(charge),"discharge_wh":_text(discharge),"round_trip_efficiency":None if efficiency is None else _text(efficiency)}

