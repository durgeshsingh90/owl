"use strict";
const trackerDates = (() => {
  function day(value) { const d = new Date(value); return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
  function shift(value, amount) { const d = day(value); d.setDate(d.getDate() + amount); return d; }
  function key(value) { const d = new Date(value); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
  function preset(name, now = new Date()) {
    const today = day(now), end = shift(today, 1);
    const starts = {today, yesterday:shift(today,-1), 'before-yesterday':shift(today,-2), week:shift(today,-((today.getDay()+6)%7)), last7:shift(today,-6), month:new Date(today.getFullYear(),today.getMonth(),1), last30:shift(today,-29), year:new Date(today.getFullYear(),0,1)};
    if(name === 'all') return {start:null,end:null};
    return {start:starts[name], end:name==='yesterday'?today:name==='before-yesterday'?shift(today,-1):end};
  }
  function custom(start,end) {
    const parse = value => {
      if(!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw Error('Choose a valid start and end date.');
      const [y,m,d]=value.split('-').map(Number), date=new Date(y,m-1,d);
      if(key(date)!==value) throw Error('Choose a valid start and end date.');
      return date;
    };
    const first=parse(start),last=parse(end);
    if(last<first) throw Error('The end date must be on or after the start date.');
    return {start:first,end:shift(last,1)};
  }
  function inRange(value,range) {
    if(!range.start && !range.end) return true;
    if(!value) return false;
    const date=new Date(value);
    return Number.isFinite(date.getTime()) && (!range.start || date>=range.start) && (!range.end || date<range.end);
  }
  function group(value, now = new Date()) {
    if(!value || !Number.isFinite(new Date(value).getTime())) return 'Date unavailable';
    const d=day(value),today=day(now);
    if(d>today) return 'Future dated';
    if(+d===+today) return 'Today';
    if(+d===+shift(today,-1)) return 'Yesterday';
    if(+d===+shift(today,-2)) return 'Day before yesterday';
    if(d>=preset('week',now).start) return 'This week';
    if(d>=preset('month',now).start) return 'This month';
    return d.getFullYear()===today.getFullYear() ? d.toLocaleDateString(undefined,{month:'long',year:'numeric'}) : String(d.getFullYear());
  }
  return {preset,custom,inRange,group,key};
})();
if(typeof module!=='undefined') module.exports=trackerDates;
