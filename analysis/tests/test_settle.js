function settleMarket(market,awayScore,homeScore,f5Away,f5Home){
  const a=Number(awayScore),h=Number(homeScore);
  if(!Number.isFinite(a)||!Number.isFinite(h))return null;
  const tot=a+h,diff=a-h;
  const num=str=>parseFloat(str);
  if(market.startsWith("f5_")){
    if(f5Away==null||f5Home==null)return null;
    const t5=Number(f5Away)+Number(f5Home);
    const line=num(market.split("_").pop());
    return market.includes("over")?t5>line:t5<line;
  }
  if(market.startsWith("over_"))return tot>num(market.slice(5));
  if(market.startsWith("under_"))return tot<num(market.slice(6));
  const [side,...rest]=market.split("_");
  const r=rest.join("_");
  const my=side==="home"?h:a;
  const opp=side==="home"?a:h;
  const md=my-opp;
  if(r==="win")return md>0;
  if(r==="cover_m15")return md>=2;
  if(r==="cover_p15")return md>=-1;
  if(r==="cover_m25")return md>=3;
  if(r==="cover_p25")return md>=-2;
  if(r.startsWith("tt_over_"))return my>num(r.slice(8));
  if(r.startsWith("tt_under_"))return my<num(r.slice(9));
  return null;
}


const cases=[
 ["over_8.5",5,4,null,null,true],
 ["over_8.5",4,4,null,null,false],
 ["under_8.5",4,4,null,null,true],
 ["under_7.5",4,4,null,null,false],
 ["home_win",3,5,null,null,true],
 ["home_win",5,3,null,null,false],
 ["away_win",5,3,null,null,true],
 ["away_cover_m15",5,3,null,null,true],
 ["away_cover_m15",4,3,null,null,false],
 ["home_cover_p15",4,3,null,null,true],
 ["home_cover_p15",5,3,null,null,false],
 ["away_cover_m25",6,3,null,null,true],
 ["home_cover_p25",5,3,null,null,true],
 ["home_tt_over_3.5",2,4,null,null,true],
 ["home_tt_over_3.5",2,3,null,null,false],
 ["away_tt_under_4.5",4,7,null,null,true],
 ["away_tt_under_4.5",5,7,null,null,false],
 ["f5_over_4.5",9,9,2,3,true],
 ["f5_over_4.5",9,9,2,2,false],
 ["f5_under_4.5",9,9,2,2,true],
];
let bad=0;
for(const [m,a,h,fa,fh,want] of cases){
  const got=settleMarket(m,a,h,fa,fh);
  if(got!==want){console.log("FAIL",m,a,h,fa,fh,"got",got,"want",want);bad++}
}
console.log(bad===0?`所有 ${cases.length} 個結算測試通過`:`${bad} 個失敗`);
