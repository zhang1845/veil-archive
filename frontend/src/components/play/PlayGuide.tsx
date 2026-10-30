const steps = [
  {
    title: "选好剧本，再选一个角色",
    text: "在剧本库点击剧本卡片，选择角色，再点“以此角色开始”。你会从这个角色的视角进入故事。",
    tip: "第一次体验《雨停之前的第七码》，建议选择档案顾问“沈雾”。",
  },
  {
    title: "读开场，也读自己的私本",
    text: "先阅读“故事开场”，再看“你的私本”，记住自己知道的事实、秘密和任务。区分亲眼所见、人物说法与推测。",
    tip: "标注“仅本人可见”的材料属于你的角色；需要分享的材料会提供“公开这条材料”按钮。",
  },
  {
    title: "进入调查，搜查每一处地点",
    text: "读完后，在“阶段进度”点“进入下一阶段”，再点“确认推进”。进入调查阶段后，点击地点按钮，解锁物证与口供。",
    tip: "《第七码》有4点调查预算，四处地点各消耗1点。全部搜完，才可以继续到终局。",
  },
  {
    title: "比对说法，写下你的判断",
    text: "把“公开信息”里的口供与“公开物证”逐条对照，找出时间、行动和说法之间的矛盾。在“交流记录”中选择“公开发言”，保存自己的推理。",
    tip: "公开试玩和本地试玩通过搜证取得封存口供，未启用即时AI盘问。你仍可凭完整线索完成推理。",
  },
  {
    title: "准备好后，再揭晓结局",
    text: "完成调查后，确认进入终局。先想清作案者、手法和证据依据，再点“结束并揭晓真相”和“确认结算”，对照结局复盘。",
    tip: "结算会显示谜底。建议先留下自己的判断，再看看哪些证据支持了它。",
  },
];

const questions = [
  {
    title: "推进按钮为什么是灰色？",
    text: "先完成本阶段要求的调查，并检查需要公开的材料。操作正在处理时稍候，页面会更新可用按钮。",
  },
  {
    title: "为什么不能向AI角色提问？",
    text: "标为“公开试玩版”或“本地试玩版”的剧本未启用实时AI对话。先搜证，再读封存口供，并用“公开发言”记录推理。支持角色提问的版本会另行明确标注。",
  },
  {
    title: "中途退出后，怎么继续？",
    text: "操作成功后，进度会保存在本局。使用同一账号，在“我的手记”选择这局继续；刷新页面也可恢复进度。",
  },
];

export function PlayGuide() {
  return (
    <>
      <section
        id="tutorial-steps"
        className="mt-10 scroll-mt-8"
        aria-labelledby="tutorial-steps-title"
      >
        <p className="eyebrow">HOW TO PLAY / 操作顺序</p>
        <h2
          id="tutorial-steps-title"
          className="mt-3 font-display text-3xl text-ink"
        >
          跟着五步，完成第一次调查。
        </h2>
        <ol className="mt-6 grid grid-cols-1 gap-5 lg:grid-cols-2">
          {steps.map((step, index) => (
            <li
              key={step.title}
              className={`panel ${index === steps.length - 1 ? "lg:col-span-2" : ""}`}
            >
              <div className="flex items-start gap-4">
                <span className="flex h-10 w-10 shrink-0 items-center justify-center border border-accent/40 font-mono text-sm text-accent">
                  0{index + 1}
                </span>
                <div className="min-w-0">
                  <h3 className="text-lg leading-7 text-ink">{step.title}</h3>
                  <p className="mt-3 text-sm leading-7 text-body">
                    {step.text}
                  </p>
                  <p className="mt-4 border-l-2 border-line pl-3 text-xs leading-6 text-muted">
                    {step.tip}
                  </p>
                </div>
              </div>
            </li>
          ))}
        </ol>
      </section>
      <section className="mt-10" aria-labelledby="tutorial-faq-title">
        <h2 id="tutorial-faq-title" className="font-display text-2xl text-ink">
          遇到这些情况时
        </h2>
        <div className="mt-5 space-y-3">
          {questions.map((question) => (
            <details key={question.title} className="panel">
              <summary className="cursor-pointer text-sm leading-7 text-ink">
                {question.title}
              </summary>
              <p className="mt-3 text-sm leading-7 text-muted">
                {question.text}
              </p>
            </details>
          ))}
        </div>
      </section>
    </>
  );
}
