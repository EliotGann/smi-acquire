import React, {useEffect, useState} from 'react';

function usePage(request, path, revision) {
    const [result, setResult] = useState({});
    useEffect(() => {
        if (!path) return;
        const controller = new AbortController();
        (async () => {
            try {
                const page = await (await request(path, controller.signal)).json();
                if (!controller.signal.aborted) setResult({path, revision, page});
            } catch (err) {
                if (!controller.signal.aborted) setResult({path, revision, error:err.message});
            }
        })();
        return () => controller.abort();
    }, [request, path, revision]);
    return result.path === path && result.revision === revision ? result : {};
}

function Pager({result, change}) {
    const {page,error} = result;
    if (!page) return <small>{error || 'Loading…'}</small>;
    return <small>{page.total} records{page.missing > 0 && ` · ${page.missing} missing`}
        {page.offset > 0 && <button onClick={()=>change(page.offset-page.page_size)}>Previous page</button>}
        {page.offset+page.page_size < page.total && <button onClick={()=>change(page.offset+page.page_size)}>Next page</button>}</small>;
}

export default function Bookmarks({request, state}) {
    const [holder, chooseHolder] = useState(null), [sample, chooseSample] = useState(null);
    const [ho, setHo] = useState(0), [so,setSo] = useState(0), [version,refresh] = useState(0);
    const [name,setName] = useState(''), [field,setField] = useState('notes'), [text,setText] = useState('');
    const [sending,setSending] = useState(false), [notice,setNotice] = useState('');
    const cmd = state?.command?.plan === 'load_sample' ? state.command : null;
    const revision = `${version}:${cmd?.state === 'finished' ? cmd.uid : ''}`;
    const enabled = !!state?.bookmarks_enabled;
    const holders = usePage(request, enabled ? `bookmarks?offset=${ho}` : null, revision);
    const samples = usePage(request, enabled && holder ? `bookmarks?holder=${encodeURIComponent(holder.id)}&offset=${so}` : null, revision);
    const blocked = sending || !state?.connected || !state.idle ||
        ['sending','accepted','unknown'].includes(state?.command?.state);
    const selected = samples.page?.rows.find(s => s.id === sample?.id);
    const select = s => {chooseSample(s); setText(s ? String(s.md?.[field] ?? '') : '');};
    const selectHolder = h => {chooseHolder(h); select(null); setSo(0);};
    async function submit(spec) {
        setSending(true); setNotice(spec.op==='goto'?'Requesting worker motion…':'Saving bookmark…');
        try {
            const reply = await (await request('bookmark', undefined, 'POST', spec)).json();
            setNotice(`${spec.op}: ${reply.state} · ${reply.sample_id || reply.holder_id || reply.tag}`);
            if (reply.state==='saved') refresh(v=>v+1);
        } catch (err) {setNotice(err.message);}
        finally {setSending(false);}
    }
    if (!enabled) return <p>Launch with <code>--bookmarks</code> to browse shared holders and capture samples.</p>;
    return <div className="bookmark-layout"><aside aria-label="Holder and sample sidebar"><h2>Holders & samples</h2>
        <button onClick={()=>refresh(v=>v+1)}>Refresh records</button><Pager result={holders} change={offset=>{setHo(offset);selectHolder(null);}}/>
        {holders.page?.rows.map(h=><div key={h.id}><button aria-expanded={h.id===holder?.id}
            onClick={()=>selectHolder(h.id===holder?.id?null:h)}>{h.name} ({h.count})</button>
            {holder?.id===h.id && <ul>{samples.page?.rows.map(s=><li key={s.id}><button aria-current={s.id===sample?.id?'true':undefined}
                onClick={()=>select(s)}>{s.name}{s.id===samples.page.active?' · active':''}</button></li>)}
                <li><Pager result={samples} change={offset=>{setSo(offset);select(null);}}/></li></ul>}</div>)}
        </aside><section><h2>Bookmark operations</h2><p>{state.mode==='simulation'?'In-memory simulation':'Shared Redis db 2'} · Edits do not move the stage.</p>
        <label>Name <input maxLength={128} value={name} onChange={e=>setName(e.target.value)}/></label>
        <button disabled={sending || !name.trim()} onClick={()=>submit({op:'holder',name})}>Create holder</button>
        <button disabled={blocked || !holder || !samples.page || !name.trim()} onClick={()=>submit({op:'capture',name,holder_id:holder.id})}>
            Capture new sample here{holder ? ` on ${holder.name}`:''}</button>
        {selected && <><h3>{selected.name}</h3><small>Sample ID: {selected.id}</small>
            <details open><summary>Nominal position</summary><pre>{JSON.stringify(selected.nominal,null,2)}</pre></details>
            {selected.refined && <details><summary>Refined position</summary><pre>{JSON.stringify(selected.refined,null,2)}</pre></details>}
            <pre>{JSON.stringify(selected.md,null,2)}</pre>
            <label>Field <select value={field} onChange={e=>{setField(e.target.value);setText(String(selected.md[e.target.value]??''));}}>
            {['notes','project_name','priority'].map(k=><option key={k}>{k}</option>)}</select></label>
            <label>Value <input value={text} maxLength={2000} onChange={e=>setText(e.target.value)}/></label>
            {selected.updated!==sample.updated && <p>Record changed. Select the sample again before editing.</p>}
            <button disabled={sending || selected.updated!==sample.updated || (field==='priority'&&!/^-?\d+$/.test(text))}
                onClick={()=>submit({op:'metadata',holder_id:holder.id,sample_id:sample.id,updated:sample.updated,md:{[field]:field==='priority'?Number(text):text}})}>Save field</button>
            <button disabled={sending || selected.updated!==sample.updated} onClick={()=>submit({op:'active',holder_id:holder.id,sample_id:sample.id,updated:sample.updated})}>Set active intent (no move)</button></>}
        {selected && <><button disabled={blocked || selected.updated!==sample.updated || !!selected.refined}
            onClick={()=>submit({op:'assign',holder_id:holder.id,sample_id:sample.id,updated:sample.updated})}>Replace nominal with current position</button>
            {state.bookmark_goto_max_um && <button disabled={blocked || selected.updated!==sample.updated || samples.page.mounted!==holder.id ||
                !state.capabilities?.plans.includes('load_sample') || (selected.refined||selected.nominal)?.frame!=='lab'}
                onClick={()=>submit({op:'goto',holder_id:holder.id,sample_id:sample.id,updated:sample.updated})}>Go to stored piezo XYZ (max {state.bookmark_goto_max_um} µm per axis)</button>}</>}
        <output>{notice}</output>{cmd && <p role="status">{cmd.value}: {cmd.state} {cmd.outcome} {cmd.message}<small>{cmd.uid}</small></p>}</section></div>;
}
