import { useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  AlertTriangle,
  BookOpen,
  Calculator,
  CheckCircle2,
  ClipboardList,
  Database,
  Download,
  FileSpreadsheet,
  FileText,
  HelpCircle,
  Info,
  Lightbulb,
  ListChecks,
  Printer,
  Target,
  Users,
} from 'lucide-react';
import { Button } from './ui/button';

type AppSection =
  | 'pricing-workflow'
  | 'pricelists'
  | 'competitors'
  | 'lists'
  | 'universal-lists'
  | 'references'
  | 'competitor-domain';

type GuideSection = {
  id: string;
  title: string;
  group: string;
};

type GuideProps = {
  onNavigate: (section: AppSection) => void;
};

const sections: GuideSection[] = [
  { id: 'intro', title: 'Что делает система', group: 'Основы' },
  { id: 'quick', title: 'Быстрый переход', group: 'Основы' },
  { id: 'references', title: 'Справочники', group: 'Подготовка данных' },
  { id: 'competitors', title: 'Конкуренты и ценовой формат', group: 'Подготовка данных' },
  { id: 'emit', title: 'Персентили Emit', group: 'Подготовка данных' },
  { id: 'lists', title: 'Работа со списками', group: 'Правила расчета' },
  { id: 'generation', title: 'Формирование цены', group: 'Расчет' },
  { id: 'results', title: 'Понимание результата', group: 'Расчет' },
  { id: 'export', title: 'Экспорт', group: 'Завершение' },
  { id: 'troubleshooting', title: 'Решение проблем', group: 'Помощь' },
];

const quickLinks: Array<{ title: string; text: string; icon: typeof Database; target: AppSection }> = [
  { title: 'Справочники', text: 'Шаблоны, загрузка себестоимости и остатков', icon: Database, target: 'references' },
  { title: 'Конкуренты', text: 'Назначение прайс-листов конкурентов', icon: ClipboardList, target: 'competitors' },
  { title: 'Персентили', text: 'Emit и статистические цены', icon: Users, target: 'competitor-domain' },
  { title: 'Работа со списками', text: 'Ограничения, фиксированные цены и исключения', icon: ListChecks, target: 'lists' },
  { title: 'Формирование цены', text: 'Пошаговый запуск расчета', icon: Calculator, target: 'pricing-workflow' },
  { title: 'Сформированные прайс-листы', text: 'Проверка результата и логов', icon: FileText, target: 'pricelists' },
  { title: 'Экспорт', text: 'Выгрузка итогового Excel', icon: Download, target: 'pricelists' },
];

const listTypes = [
  {
    name: 'Фиксированная цена',
    text: 'Система ставит указанную цену как итоговую. Это правило используют, когда цена уже согласована вручную и не должна изменяться из-за конкурентов или прогиба.',
    note: 'Если у товара нет себестоимости, расчет блокируется даже при фиксированной цене.',
  },
  {
    name: 'Фиксированная наценка',
    text: 'Система рассчитывает итоговую цену от себестоимости по проценту из списка. Конкуренты в этом случае не выбирают итоговую цену.',
    note: 'Подходит для товаров, где нужна стабильная маржа независимо от рынка.',
  },
  {
    name: 'Критическая наценка',
    text: 'Если у товара есть цены конкурентов, значение из списка заменяет обычную маржу для расчета МДЦ. Если цен конкурентов нет вообще, применяется глобальная маржа для товаров без конкурентов.',
    note: 'Если конкуренты есть, но все ниже МДЦ, критическая наценка все равно применяется.',
  },
  {
    name: 'Минимальная цена',
    text: 'Итоговая цена не может быть ниже указанного значения. Используется как нижний ручной ограничитель.',
    note: 'Если рассчитанная цена выше минимума, список только фиксируется в логе и не меняет цену.',
  },
  {
    name: 'Максимальная цена',
    text: 'Итоговая цена не может быть выше указанного значения. Используется для ограничения цены сверху.',
    note: 'Полезно для социально чувствительных товаров или ручных коммерческих ограничений.',
  },
  {
    name: 'Без прогиба',
    text: 'Отключает снижение цены относительно конкурента. Система может видеть конкурента, но не уменьшает цену на процент прогиба.',
    note: 'Используйте, когда товар не нужно делать дешевле конкурента.',
  },
  {
    name: 'Исключить из переоценки',
    text: 'Товар не участвует в формировании прайс-листа и не должен попадать в итоговую выгрузку.',
    note: 'Используйте для товаров, которые временно нельзя переоценивать.',
  },
];

const resultTerms = [
  ['Итоговая цена', 'Цена, которую система предлагает использовать в сформированном прайс-листе.'],
  ['Себестоимость', 'Закупочная или учетная стоимость товара. Без нее расчет не выполняется.'],
  ['МДЦ', 'Минимальная допустимая цена. Это нижняя граница, рассчитанная от себестоимости и маржи.'],
  ['Цена конкурента', 'Цена выбранного конкурента или минимальная цена, используемая для сравнения.'],
  ['Цена после прогиба', 'Цена конкурента после уменьшения на процент прогиба.'],
  ['Левое плечо', 'Наша цена ниже цены конкурента.'],
  ['Зона логичности', 'Наша цена находится рядом с конкурентом, обычно в пределах допустимого отклонения.'],
  ['Правое плечо', 'Наша цена заметно выше конкурента. Требует внимания.'],
  ['Нет данных', 'Нет цены конкурента для сравнения или расчет был заблокирован данными.'],
];

const troubleshooting = [
  {
    title: 'Нет себестоимости',
    symptoms: ['Итоговая цена равна 0.', 'В логе указано: «Нет себестоимости, расчет не выполнен».', 'МДЦ пустая или равна 0.'],
    causes: ['Не загружен справочник себестоимости.', 'В файле себестоимости пустое значение.', 'В себестоимости указано 0, отрицательное число или текст вместо числа.'],
    solution: ['Откройте раздел «Справочники».', 'Скачайте шаблон себестоимости через «Скачать шаблон».', 'Заполните колонку «Себестоимость» положительным числом.', 'Загрузите файл заново и повторите расчет.'],
  },
  {
    title: 'Персентили не отображаются',
    symptoms: ['В выборе персентиля нет P10/P20/P30/P40/P60.', 'Emit есть в системе, но расчет не видит статистические цены.'],
    causes: ['Emit не назначен на выбранный ценовой формат.', 'Персентили еще не пересчитаны.', 'Выбран другой ценовой формат.'],
    solution: ['Откройте раздел «Конкуренты».', 'Проверьте назначение Emit для нужного ценового формата.', 'Обновите данные конкурентов или пересчитайте персентили.', 'Вернитесь к формированию цены.'],
  },
  {
    title: 'Emit не привязан к ценовому формату',
    symptoms: ['Цены Emit не участвуют в расчете.', 'В процессе формирования цены нет источника Emit.', 'Персентили пустые.'],
    causes: ['Источник не выбран в назначении конкурентов.', 'Назначение сделано для другого филиала или формата.'],
    solution: ['Откройте «Конкуренты».', 'Выберите нужный филиал и ценовой формат.', 'Отметьте Emit как активный источник.', 'Сохраните назначение и обновите цены.'],
  },
  {
    title: 'Список не применяется',
    symptoms: ['В результате нет лога списка.', 'Цена не изменилась, хотя товар есть в файле списка.', 'Список виден, но не влияет на расчет.'],
    causes: ['Список неактивен.', 'Период действия не включает дату расчета.', 'Список привязан к другому ценовому формату.', 'SKU в списке отличается от SKU товара.', 'Есть конфликт двух активных списков одного типа.'],
    solution: ['Проверьте статус списка.', 'Проверьте дату начала и окончания.', 'Проверьте привязку к ценовому формату.', 'Сверьте SKU.', 'Оставьте только одно активное правило одного типа для товара.'],
  },
  {
    title: 'Цена равна 0',
    symptoms: ['В сформированном прайс-листе итоговая цена 0.', 'Нет цены после прогиба.', 'Нет зоны сравнения.'],
    causes: ['Нет себестоимости.', 'Товар исключен из переоценки.', 'Данные товара некорректны.'],
    solution: ['Сначала проверьте лог расчета.', 'Если указано отсутствие себестоимости, загрузите справочник себестоимости.', 'Если товар исключен списком, проверьте список «Исключить из переоценки».'],
  },
  {
    title: 'Импорт не проходит',
    symptoms: ['Статус импорта «Ошибка» или «Частично».', 'Часть строк не загружена.', 'В истории импорта есть ошибки по строкам.'],
    causes: ['Не выбран филиал.', 'Пустой SKU.', 'Неверный формат числа.', 'Файл не соответствует шаблону.'],
    solution: ['Скачайте новый шаблон.', 'Не меняйте первую строку.', 'Проверьте SKU и числовые поля.', 'Загрузите файл повторно.'],
  },
  {
    title: 'Файл имеет неверный формат',
    symptoms: ['Система пишет, что обязательные колонки не найдены.', 'Файл открывается, но строки не импортируются.'],
    causes: ['Переименованы колонки.', 'Загружен CSV вместо XLSX.', 'В первой строке нет заголовков.', 'Шаблон был скопирован в другую структуру.'],
    solution: ['Используйте только XLSX.', 'Скачайте шаблон из системы.', 'Заполняйте данные со второй строки.', 'Не объединяйте ячейки и не добавляйте служебные строки над заголовками.'],
  },
  {
    title: 'Нет цен конкурентов',
    symptoms: ['Зона результата «Нет данных».', 'В логе указано, что цен выбранных конкурентов нет.', 'Цена рассчитана по марже для товаров без конкурентов.'],
    causes: ['Конкуренты не назначены.', 'Нет совпадения товара по SKU или коду.', 'Источник конкурента пустой или устарел.'],
    solution: ['Проверьте назначение конкурентов.', 'Проверьте наличие товара в прайс-листе конкурента.', 'Обновите источник конкурента.', 'Если конкурентов действительно нет, проверьте диапазоны маржи для товаров без конкурентов.'],
  },
];

function scrollToGuideSection(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function StepList({ items }: { items: string[] }) {
  return (
    <ol className="guide-steps">
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ol>
  );
}

function AppLink({ children, target, onNavigate }: { children: ReactNode; target: AppSection; onNavigate: (section: AppSection) => void }) {
  return (
    <button type="button" className="guide-app-link" onClick={() => onNavigate(target)}>
      {children}
    </button>
  );
}

function InfoBlock({
  tone = 'info',
  title,
  children,
}: {
  tone?: 'info' | 'warning' | 'tip' | 'success';
  title: string;
  children: ReactNode;
}) {
  const Icon = tone === 'warning' ? AlertTriangle : tone === 'tip' ? Lightbulb : tone === 'success' ? CheckCircle2 : Info;
  return (
    <div className={`guide-info ${tone}`}>
      <Icon className="h-5 w-5" />
      <div>
        <strong>{title}</strong>
        <div>{children}</div>
      </div>
    </div>
  );
}

function ExampleCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="guide-example">
      <div className="guide-example-title">
        <Target className="h-4 w-4" />
        {title}
      </div>
      {children}
    </div>
  );
}

function GuideSectionBlock({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="guide-section">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

export function UserGuideTab({ onNavigate }: GuideProps) {
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({
    Основы: true,
    'Подготовка данных': true,
    'Правила расчета': true,
    Расчет: true,
    Завершение: true,
    Помощь: true,
  });
  const [activeSection, setActiveSection] = useState(sections[0].id);

  const groupedSections = useMemo(() => {
    return sections.reduce<Record<string, GuideSection[]>>((acc, section) => {
      acc[section.group] = [...(acc[section.group] || []), section];
      return acc;
    }, {});
  }, []);

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (visible?.target.id) setActiveSection(visible.target.id);
      },
      { rootMargin: '-20% 0px -65% 0px', threshold: [0.1, 0.25, 0.5] }
    );

    sections.forEach((section) => {
      const element = document.getElementById(section.id);
      if (element) observer.observe(element);
    });

    return () => observer.disconnect();
  }, []);

  return (
    <div className="guide-shell">
      <aside className="guide-sidebar">
        <div className="guide-sidebar-title">Содержание</div>
        {Object.entries(groupedSections).map(([group, items]) => (
          <div className="guide-nav-group" key={group}>
            <button
              type="button"
              className="guide-nav-group-button"
              onClick={() => setOpenGroups((prev) => ({ ...prev, [group]: !prev[group] }))}
            >
              <span>{group}</span>
              <span>{openGroups[group] ? '−' : '+'}</span>
            </button>
            {openGroups[group] ? (
              <div className="guide-nav-links">
                {items.map((section) => (
                  <button
                    type="button"
                    key={section.id}
                    onClick={() => scrollToGuideSection(section.id)}
                    className={activeSection === section.id ? 'active' : ''}
                  >
                    {section.title}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </aside>

      <article className="guide-document">
        <header className="guide-hero">
          <div>
            <div className="guide-kicker">
              <BookOpen className="h-4 w-4" />
              Руководство пользователя
            </div>
            <h1>Руководство для сотрудников аптеки</h1>
            <p>
              Это практическая инструкция по работе с системой: как подготовить данные, назначить конкурентов,
              настроить списки, сформировать цены, проверить результат и выгрузить прайс-лист.
            </p>
          </div>
          <Button type="button" variant="outline" onClick={() => window.print()} className="guide-print">
            <Printer className="mr-2 h-4 w-4" />
            Печать / PDF
          </Button>
        </header>

        <GuideSectionBlock id="intro" title="1. Что делает система">
          <p>
            Система помогает аптеке формировать цены не вручную, а по единым правилам. Она учитывает себестоимость,
            остатки, цены конкурентов, правила наценки, прогиб под конкурента, ограничения из списков и настройки
            выбранного ценового формата.
          </p>
          <InfoBlock title="Зачем это нужно">
            <p>
              Главная задача системы — сделать расчет прозрачным. Пользователь должен видеть не только итоговую цену,
              но и причину: цена взята по конкуренту, поднята до МДЦ, ограничена списком или не рассчитана из-за
              отсутствия данных.
            </p>
          </InfoBlock>
          <div className="guide-flow">
            <span>Справочники</span>
            <span>Конкуренты</span>
            <span>Списки</span>
            <span>Расчет</span>
            <span>Проверка</span>
            <span>Экспорт</span>
          </div>
          <p>
            Рабочий процесс обычно такой: загрузить <AppLink target="references" onNavigate={onNavigate}>справочники</AppLink>,
            проверить <AppLink target="competitors" onNavigate={onNavigate}>конкурентов</AppLink>, настроить
            <AppLink target="lists" onNavigate={onNavigate}> работу со списками</AppLink>, запустить
            <AppLink target="pricing-workflow" onNavigate={onNavigate}> формирование цены</AppLink>, открыть
            <AppLink target="pricelists" onNavigate={onNavigate}> сформированный прайс-лист</AppLink> и выполнить экспорт.
          </p>
        </GuideSectionBlock>

        <GuideSectionBlock id="quick" title="2. Быстрый переход">
          <div className="guide-quick-grid">
            {quickLinks.map((item) => {
              const Icon = item.icon;
              return (
                <button type="button" key={item.title} className="guide-quick-card" onClick={() => onNavigate(item.target)}>
                  <Icon className="h-5 w-5" />
                  <strong>{item.title}</strong>
                  <span>{item.text}</span>
                </button>
              );
            })}
          </div>
        </GuideSectionBlock>

        <GuideSectionBlock id="references" title="3. Справочники">
          <p>
            Справочники — это исходные данные для расчета. Если справочники неполные или загружены в неправильном
            формате, система либо не рассчитает цену, либо рассчитает ее не так, как ожидается. Основные справочники
            для ежедневной работы: себестоимость и остатки.
          </p>
          <InfoBlock tone="warning" title="Себестоимость обязательна">
            <p>
              Если себестоимость отсутствует, равна 0, отрицательная или не распознана как число, цена не
              рассчитывается. В результате будет итоговая цена 0 и лог: «Нет себестоимости, расчет не выполнен».
            </p>
          </InfoBlock>
          <StepList
            items={[
              'Откройте раздел «Справочники».',
              'Выберите тип файла: «Себестоимость» для себестоимости или «Остаток» для остатков.',
              'Нажмите «Скачать шаблон» и заполните скачанный XLSX.',
              'Не меняйте первую строку с названиями колонок.',
              'Выберите филиал или несколько филиалов.',
              'Нажмите «Загрузить файл» и проверьте статус импорта.',
            ]}
          />
          <div className="guide-table-wrap">
            <table className="guide-table">
              <thead>
                <tr>
                  <th>Справочник</th>
                  <th>Обязательные колонки</th>
                  <th>Где используется</th>
                  <th>Частые ошибки</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Себестоимость</td>
                  <td>SKU, Производитель, Наименование, Себестоимость</td>
                  <td>Расчет МДЦ, маржи и итоговой цены.</td>
                  <td>Пустая себестоимость, 0, текст вместо числа, измененные заголовки.</td>
                </tr>
                <tr>
                  <td>Остаток</td>
                  <td>SKU, Производитель, Наименование, Остаток</td>
                  <td>Проверка наличия и аналитика по товарам.</td>
                  <td>Пустой SKU, неверный филиал, текст в колонке «Остаток».</td>
                </tr>
              </tbody>
            </table>
          </div>
          <ExampleCard title="Пример заполнения себестоимости">
            <p>SKU: 12345, производитель: Пример Фарма, товар: Препарат 10 мг, себестоимость: 150.</p>
            <p>Если себестоимость = 150 и маржа 20%, МДЦ считается как 150 / (1 − 0,20) = 187,50.</p>
          </ExampleCard>
          <InfoBlock tone="tip" title="Совет">
            <p>
              Если импорт не проходит, скачайте новый шаблон из раздела
              <AppLink target="references" onNavigate={onNavigate}> «Справочники»</AppLink> и перенесите данные в него.
              Так вы исключите ошибки в структуре файла.
            </p>
          </InfoBlock>
        </GuideSectionBlock>

        <GuideSectionBlock id="competitors" title="4. Конкуренты и ценовой формат">
          <p>
            Прайс-листы конкурентов — это источники внешних цен. Они нужны, чтобы система могла сравнить нашу цену с
            рынком и при необходимости поставить цену с прогибом ниже конкурента, но не ниже МДЦ.
          </p>
          <div className="guide-two-col">
            <InfoBlock title="Ценовой формат">
              <p>
                Ценовой формат — это набор настроек для филиала или сценария: правила маржи, прогибы, конкуренты,
                персентили и списки. Всегда проверяйте, что работаете с правильным форматом.
              </p>
            </InfoBlock>
            <InfoBlock tone="tip" title="Обычные конкуренты и Emit">
              <p>
                Обычный конкурент чаще дает одну цену по товару. Emit может давать несколько цен, поэтому для него
                используются персентили.
              </p>
            </InfoBlock>
          </div>
          <StepList
            items={[
              'Откройте раздел «Конкуренты».',
              'Выберите нужный филиал и ценовой формат.',
              'Отметьте источники конкурентов, которые должны участвовать в расчете.',
              'Проверьте, что у источников есть товары и цены.',
              'Сохраните назначение и обновите данные при необходимости.',
            ]}
          />
          <p>
            Если конкурент не назначен на ценовой формат, его цены не попадут в расчет даже тогда, когда файл конкурента
            загружен в систему.
          </p>
        </GuideSectionBlock>

        <GuideSectionBlock id="emit" title="5. Персентили Emit">
          <p>
            У Emit по одному товару может быть несколько цен. Например, один и тот же SKU встречается в разных точках
            или у разных поставщиков. Чтобы не выбирать случайную цену, система считает статистические персентили.
          </p>
          <div className="guide-grid">
            {[
              ['P10', 'Низкий ориентир. 10% цен Emit ниже или равны этому значению.'],
              ['P20', 'Более мягкий, но все еще конкурентный ориентир.'],
              ['P30', 'Умеренно конкурентный ориентир.'],
              ['P40', 'Ближе к средней рыночной зоне.'],
              ['P60', 'Более высокий ориентир, когда не нужно сильно снижать цену.'],
            ].map(([name, text]) => (
              <div className="guide-mini-card" key={name}>
                <strong>{name}</strong>
                <span>{text}</span>
              </div>
            ))}
          </div>
          <InfoBlock title="Важно">
            <p>
              Персентили принадлежат ценовому формату, а не конкретному сформированному прайс-листу. Если вы сменили
              ценовой формат, набор персентилей может быть другим.
            </p>
          </InfoBlock>
          <ExampleCard title="Пример персентиля">
            <p>По товару найдено 10 цен Emit: 170, 172, 175, 178, 180, 185, 190, 195, 200, 210.</p>
            <p>P10 будет ближе к нижней цене, P60 — ближе к верхней середине. Чем ниже персентиль, тем агрессивнее цена.</p>
          </ExampleCard>
        </GuideSectionBlock>

        <GuideSectionBlock id="lists" title="6. Работа со списками">
          <p>
            Списки нужны для ручных исключений и специальных правил. Они применяются к конкретным товарам и помогают
            учесть коммерческие договоренности, ограничения, акции или запреты на переоценку.
          </p>
          <div className="guide-list-type-grid">
            {listTypes.map((item) => (
              <div className="guide-mini-card" key={item.name}>
                <strong>{item.name}</strong>
                <span>{item.text}</span>
                <em>{item.note}</em>
              </div>
            ))}
          </div>
          <details open>
            <summary>Приоритет правил</summary>
            <ol>
              <li>Если нет себестоимости, расчет блокируется до применения любых списков.</li>
              <li>«Исключить из переоценки» убирает товар из расчета.</li>
              <li>«Фиксированная цена» задает итоговую цену напрямую.</li>
              <li>«Фиксированная наценка» рассчитывает итоговую цену по марже списка.</li>
              <li>«Критическая наценка» применяется только если у товара есть цены конкурентов.</li>
              <li>«Минимальная цена» и «Максимальная цена» ограничивают итоговую цену.</li>
              <li>«Без прогиба» отключает снижение относительно конкурента.</li>
            </ol>
          </details>
          <StepList
            items={[
              'Откройте «Работа со списками».',
              'Создайте список и выберите тип правила.',
              'Укажите период действия и ценовой формат, если список должен работать только для одного формата.',
              'Добавьте товары по SKU и укажите значение правила.',
              'Проверьте, что статус списка активный.',
              'После формирования прайс-листа проверьте лог списка в результате.',
            ]}
          />
        </GuideSectionBlock>

        <GuideSectionBlock id="generation" title="7. Формирование цены">
          <p>
            Формирование цены выполняется пошагово. Перед запуском важно проверить готовность данных:
            справочники, конкуренты, персентили и списки.
          </p>
          <StepList
            items={[
              'Выберите филиал в левой панели.',
              'Выберите ценовой формат.',
              'Откройте «Формирование цены».',
              'Проверьте блок готовности данных.',
              'Выберите конкурентов и, если нужно, персентиль Emit.',
              'Запустите расчет.',
              'Дождитесь завершения.',
              'Откройте сформированный прайс-лист.',
            ]}
          />
          <ExampleCard title="Простой пример расчета">
            <p>SKU 12345: себестоимость = 150, цена конкурента = 180, маржа = 20%, прогиб = 1%.</p>
            <p>МДЦ = 150 / (1 − 0,20) = 187,50. Цена после прогиба = 180 × 0,99 = 178,20.</p>
            <p>Так как 178,20 ниже МДЦ, система не может поставить эту цену и поднимает итог до МДЦ.</p>
          </ExampleCard>
        </GuideSectionBlock>

        <GuideSectionBlock id="results" title="8. Понимание результата">
          <div className="guide-table-wrap">
            <table className="guide-table">
              <thead>
                <tr>
                  <th>Поле</th>
                  <th>Что означает</th>
                </tr>
              </thead>
              <tbody>
                {resultTerms.map(([term, text]) => (
                  <tr key={term}>
                    <td>{term}</td>
                    <td>{text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <InfoBlock title="Логи расчета">
            <p>
              Лог расчета объясняет, почему выбрана цена: по конкуренту, по МДЦ, по отсутствию конкурентов, по списку
              или из-за проблемы с данными. Лог списка отдельно показывает, какой список сработал и изменил ли он цену.
            </p>
          </InfoBlock>
          <InfoBlock tone="warning" title="Что проверять в первую очередь">
            <p>
              Обязательно просматривайте товары в зоне «Правое плечо» и «Нет данных». Там чаще всего находятся товары
              без конкурента, с высокой МДЦ или с неполными исходными данными.
            </p>
          </InfoBlock>
        </GuideSectionBlock>

        <GuideSectionBlock id="export" title="9. Экспорт">
          <p>
            После проверки результата откройте <AppLink target="pricelists" onNavigate={onNavigate}>сформированные
            прайс-листы</AppLink> и выполните экспорт в Excel. Выгрузка нужна для передачи цен дальше в учетную систему
            или для ручной проверки ответственным сотрудником.
          </p>
          <ul>
            <li>SKU и название помогают идентифицировать товар.</li>
            <li>Себестоимость и МДЦ показывают экономическое ограничение.</li>
            <li>Цена конкурента и цена после прогиба показывают рыночный ориентир.</li>
            <li>Итоговая цена — цена, которую система предлагает применить.</li>
            <li>Логи объясняют причину расчета и влияние списков.</li>
          </ul>
          <InfoBlock tone="tip" title="Для будущего PDF">
            <p>
              На странице руководства есть кнопка «Печать / PDF». Она использует печатную версию страницы и подходит
              для сохранения инструкции в PDF.
            </p>
          </InfoBlock>
        </GuideSectionBlock>

        <GuideSectionBlock id="troubleshooting" title="10. Решение проблем">
          <div className="guide-accordion">
            {troubleshooting.map((item) => (
              <details key={item.title}>
                <summary>
                  <HelpCircle className="h-4 w-4" />
                  {item.title}
                </summary>
                <div className="guide-trouble-grid">
                  <div>
                    <strong>Симптомы</strong>
                    <ul>{item.symptoms.map((value) => <li key={value}>{value}</li>)}</ul>
                  </div>
                  <div>
                    <strong>Возможные причины</strong>
                    <ul>{item.causes.map((value) => <li key={value}>{value}</li>)}</ul>
                  </div>
                  <div>
                    <strong>Как исправить</strong>
                    <ul>{item.solution.map((value) => <li key={value}>{value}</li>)}</ul>
                  </div>
                </div>
              </details>
            ))}
          </div>
        </GuideSectionBlock>
      </article>
    </div>
  );
}
