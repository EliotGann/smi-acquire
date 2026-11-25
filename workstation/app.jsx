import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import FinchHeader from 'finch-header';
import Bookmarks from './Bookmarks';

const token = location.hash.slice(1) || sessionStorage.getItem('session');
if (token) sessionStorage.setItem('session', token);
history.replaceState(null, '', location.pathname);

async function request(path, signal, method = 'GET', body) {
    const response = await fetch(`/api/${path}`, {method, signal, cache: 'no-store',
        body: body && JSON.stringify(body),
        headers: {'X-Workstation-Session': token || '', 'Content-Type': 'application/json'}});
    if (response.status === 409) throw Error((await response.json()).error);
    if (!response.ok) throw Error(response.status === 403 ? 'Reopen from workstation launcher' :
        `Unavailable (${response.status})`);
    return response;
}

// Await each request before scheduling another. Tabs that unmount release their subscription.
function usePoll(path, period, image = false) {
    const [value, setValue] = useState(null), [error, setError] = useState('');
    useEffect(() => {
        const controller = new AbortController();
        let timer, url, delay = period;
        async function tick() {
            try {
                const r = await request(path, controller.signal);
                const result = image ? await r.blob() : await r.json();
                if (controller.signal.aborted) return;
                if (url) URL.revokeObjectURL(url);
                setValue(image ? (url = URL.createObjectURL(result)) : result);
                setError(''); delay = period;
            } catch (err) {
                if (!controller.signal.aborted) {setValue(null); setError(err.message);}
                delay = Math.min(10000, delay * 2);
            }
            if (!controller.signal.aborted) timer = setTimeout(tick, delay);
        }
        tick();
        return () => {controller.abort(); clearTimeout(timer); if (url) URL.revokeObjectURL(url);};
    }, [path, period, image]);
    return [value, error];
}

function Camera({state}) {
    const [url, error] = usePoll('frame', 300, true);
    const [message, setMessage] = useState(''), [pending, setPending] = useState(false);
    const [seconds, setSeconds] = useState('');
    const [previewStack,setPreviewStack] = useState('piezo'), [previewText,setPreviewText] = useState('');
    const [previewBusy,setPreviewBusy] = useState(false);
    const [review,setReview] = useState(null), [xyRead,setXYRead] = useState('');
    const live = state?.mode === 'live monitor';
    async function preview(e) {
        if (!live || previewBusy || pending) return;
        setReview(null); setXYRead('');
        const img = e.currentTarget, rect = img.getBoundingClientRect();
        const width = img.naturalWidth, height = img.naturalHeight;
        if (!width || !height || !rect.width || !rect.height) return;
        const x = (e.clientX-rect.left)*width/rect.width, y = (e.clientY-rect.top)*height/rect.height;
        setPreviewBusy(true);
        try {
            const p = await (await request('click-preview?' + new URLSearchParams({stack:previewStack,x,y,width,height}))).json();
            setPreviewText(`${p.stack} at pixel (${p.x.toFixed(1)}, ${p.y.toFixed(1)}), beam (${p.center.join(', ')}), χ ${p.chi.toFixed(3)}°: Δx ${p.dx.toFixed(4)}, Δy ${p.dy.toFixed(4)} ${p.units}${p.move_note ? ' · '+p.move_note : ''}`);
            if (p.move_id) setReview(p);
        } catch (err) {setPreviewText(err.message);}
        finally {setPreviewBusy(false);}
    }
    const caps = state?.capabilities, command = state?.command;
    const ready = state?.connected && state?.idle && !pending &&
        !['sending', 'accepted', 'unknown'].includes(command?.state);
    const allowed = (plan, device) => ready && caps?.plans.includes(plan) && caps.movable.includes(device);
    async function moveXY() {
        if (!review || pending) return;
        setReview(null); setPending(true); setXYRead('');
        try {
            const cmd = await (await request('click-move', undefined, 'POST', {move_id:review.move_id})).json();
            setMessage(`XY from (${review.origin.map(v=>v.toFixed(3))}) to (${review.target.map(v=>v.toFixed(3))}) µm: ${cmd.state}`);
        } catch (err) { setMessage(err.message); }
        finally {setPending(false);}
    }
    async function readXY() {
        try {
            const {xy} = await (await request('piezo-xy')).json();
            setXYRead(`Direct readback at ${new Date().toLocaleTimeString()}: X ${xy[0].toFixed(3)}, Y ${xy[1].toFixed(3)} µm`);
        } catch (err) {setXYRead(err.message);}
    }
    const exposureBlocker = !state?.connected ? 'Waiting for QueueServer connection' :
        !caps ? 'Allowed plans and devices unavailable' :
        !caps.exposure ? 'No exposure device bound' :
        !caps.plans.includes('mv') ? 'mv is not allowed' :
        !caps.movable.includes(caps.exposure) ? `${caps.exposure} is not listed as movable` :
        pending ? 'Sending request' : !state.idle ? state.idle_blocker || 'Worker not ready' :
        !ready ? `Previous command: ${command.state}; check history` :
        state.exposure_rbv == null ? 'Exposure readback unavailable' :
        !(+seconds >= .0001 && +seconds <= 2) ? 'Enter 0.0001 to 2 seconds' : '';
    async function send(action, value, device) {
        setPending(true);
        try {
            const cmd = await (await request(action, undefined, 'POST', {value,device})).json();
            setMessage(`Request ${cmd.tag}: ${cmd.state}`);
        } catch (err) { setMessage(err.message); }
        finally {setPending(false);}
    }
    return <section><h2>On-axis camera</h2><p>{state?.mode === 'simulation' ? 'Synthetic image' : 'Direct EPICS readback'}</p>
        {live && <p><label>Click preview (no motion) <select value={previewStack} disabled={previewBusy}
            onChange={e=>{setPreviewStack(e.target.value);setPreviewText('');setReview(null);}}><option value="piezo">Piezo XY</option><option value="huber">Huber XY</option></select></label></p>}
        {url ? <img className="camera" src={url} onClick={preview} alt="Latest on-axis camera frame"/> : <p>{error || 'Waiting for a frame…'}</p>}
        {live && <output aria-live="polite">{previewBusy?'Reading chi…':previewText || 'Click an image feature to preview its displacement.'}</output>}
        {state?.click_move_enabled && previewStack==='piezo' && <fieldset><legend>Reviewed piezo XY</legend>
            {review && <p>Start ({review.origin.map(v=>v.toFixed(3)).join(', ')}) → target ({review.target.map(v=>v.toFixed(3)).join(', ')}) µm</p>}
            <button disabled={!url || !review || previewBusy || !ready || !caps?.plans.includes('move_piezo_xy')} onClick={moveXY}>
                Move piezo XY{review && ` (ΔX ${review.dx.toFixed(4)}, ΔY ${review.dy.toFixed(4)} µm)`}</button>
            {!caps?.plans.includes('move_piezo_xy') && <small>Worker plan move_piezo_xy is not available for this login.</small>}
            <button onClick={readXY}>Read X/Y now</button><output>{xyRead}</output></fieldset>}
        <fieldset><legend>Immediate plans</legend><p>Exposure readback: {state?.exposure_rbv == null ? 'unavailable' : `${state.exposure_rbv} s`}</p>
            <label>Requested exposure (s) <input type="number" min="0.0001" max="2" step="any" value={seconds} onChange={e=>setSeconds(e.target.value)}/></label>
            <button disabled={Boolean(exposureBlocker)}
                onClick={()=>send('exposure', +seconds)}>Set exposure</button>
            {exposureBlocker && <small>{exposureBlocker}</small>}
            {caps?.jogs?.map(({device,step,units})=><p key={device}><strong>{device}</strong>{[-1,1].map(sign=><button key={sign}
                aria-label={`Jog ${device} ${sign>0?'+':'−'}${step ?? ''} ${units}`} disabled={!step || !allowed('mvr',device)}
                onClick={()=>send('jog',sign*step,device)}>{sign>0?'+':'−'}{step ?? '—'} {units}</button>)}
                {!caps.movable.includes(device)?'Not advertised as movable':!step?'Set --angular-step-deg to enable':''}</p>)}
            <output>{message}</output>{command && <p role="status">{command.plan} {command.device} {command.value}: {command.state} {command.outcome} {command.message}
                <small>Item UID: {command.uid || 'not available'}{state.confirmation && ` · Exposure RBV: ${state.confirmation}`}</small></p>}
        </fieldset></section>;
}

function Capabilities({caps}) {
    const [query, setQuery] = useState('');
    if (!caps) return <p>Allowed lists unavailable.</p>;
    const devices = caps.devices.filter(d=>d.toLowerCase().includes(query.toLowerCase()));
    return <details><summary>Capabilities for this workstation login</summary>
        <p>mv: {caps.plans.includes('mv')?'allowed':'missing'} · mvr: {caps.plans.includes('mvr')?'allowed':'missing'}</p>
        {[['Exposure', caps.exposure], ...(caps.jogs?.length ? caps.jogs.map(j=>['Jog',j.device]) : [['Jog',null]])].map(([label,device])=><p key={`${label}:${device}`}>{label}: {device ? `${device} (${caps.movable.includes(device)?'movable':'not listed as movable'})` : 'not bound'}</p>)}
        <details><summary>Allowed plans ({caps.plans.length})</summary>{caps.plans.join(', ')}</details>
        <label>Find a device <input value={query} onChange={e=>setQuery(e.target.value)} placeholder="e.g. acquire_time"/></label>
        <p>{devices.length} matches (first 100)</p><pre>{devices.slice(0,100).join('\n')}</pre></details>;
}

function Items({title, items = []}) {
    return <section><h2>{title}</h2>{items.length ? <ol>{items.map(i => <li key={i.uid}>
        <strong>{i.name}</strong> {i.outcome} {i.message}<small>{i.uid}</small></li>)}</ol> : <p>No items.</p>}</section>;
}

const queueButtons = [
    ['start','Start queue'], ['stop-after','Stop after current item'], ['cancel-stop','Cancel scheduled stop'],
    ['pause-deferred','Pause at checkpoint'], ['pause-immediate','Pause immediately'],
    ['resume','Resume plan'], ['reload','Reload idle environment'],
];

function QueuePanel({state}) {
    const [busy,setBusy] = useState(false), [notice,setNotice] = useState('');
    async function issue(action) {
        setBusy(true); setNotice('Sending request…');
        try {
            const expected = ['start','reload'].includes(action) ? state.queue_revision : state.running_uid;
            const response = await request('queue-action', undefined, 'POST', {action, expected});
            setNotice((await response.json()).message);
        } catch (err) {setNotice(err.message);}
        finally {setBusy(false);}
    }
    return <section><h2>Queue & RunEngine controls</h2>
        <p>Running item: {state.running_uid ? `${state.running || 'Plan'} · ${state.running_uid}` : 'none'}
            {state.stop_pending && ' · Queue will stop after this item'}{state.pause_pending && ' · Pause requested'}</p>
        {state.queue_enabled ? <>{queueButtons.map(([action,label]) =>
            <button key={action} disabled={busy || !!state.errors.queue || !state.queue_actions?.includes(action)}
                onClick={()=>issue(action)}>{label}</button>)}
            <output aria-live="polite">{notice}</output>
            {state.queue_message !== notice && <small>{state.queue_message}</small>}</> :
            <p>Monitoring only. Launch with <code>--queue-controls</code> to enable these actions.</p>}</section>;
}

function App() {
    const [page, navigate] = useState('Camera');
    const [state, error] = usePoll('state', 1000);
    return <><FinchHeader title="SMI Acquire" pageTitle="Workstation" logoUrl="" rightSlot={<b>{state?.mode || 'Connecting'}</b>}/>
        <div className="status" role="status">{state?.connected ? `Manager: ${state.manager} · RE: ${state.re || '—'} · Worker: ${state.environment || '—'} · Pending: ${state.count} · Running: ${state.running || 'none'}` : error || 'QueueServer disconnected'}
            {Object.entries(state?.errors || {}).map(([key, msg]) => <span key={key}> · {key}: {msg} (data unavailable)</span>)}</div>
        <div className="workspace"><nav aria-label="Workstation tools"><details open><summary>Acquisition</summary>
            {Object.entries({'Align & Samples':['Camera','Bookmarks'], 'RunEngine':['Queue & Console']}).map(([section, tabs]) =>
                <details key={section} open><summary>{section}</summary>{tabs.map(tab=><button key={tab} aria-current={page === tab ? 'page' : undefined}
                    onClick={() => navigate(tab)}>{tab}</button>)}</details>)}</details></nav>
        <main><h1>{page}</h1>{page === 'Camera' ? <Camera state={state}/> : page==='Bookmarks' ? <Bookmarks request={request} state={state}/> : state?.connected ? <>
            <Capabilities caps={state.capabilities}/>
            <QueuePanel state={state}/>
            {!state.errors.queue && <Items title="Pending queue" items={state.queue}/>}
            {!state.errors.history && <Items title="Recent history" items={state.history}/>}
            <section><h2>RunEngine output</h2><pre>{state.errors.console ? 'Console unavailable' : state.console || 'Waiting for output…'}</pre></section>
        </> : <p>Waiting for QueueServer.</p>}</main></div></>;
}

createRoot(document.getElementById('root')).render(<App/>);
