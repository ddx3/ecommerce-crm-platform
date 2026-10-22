import json
from django.forms import Widget
from django.utils.html import escape
from django.utils.safestring import mark_safe


QUILL_TOOLBAR = [
    [{'size': ['10px', '12px', '14px', '16px', '18px', '20px', '24px', '28px', '32px', '36px', '48px']}, {'header': [1, 2, 3, 4, 5, 6, False]}],
    ['bold', 'italic', 'underline', 'strike'],
    [{'script': 'super'}, {'script': 'sub'}],
    [{'color': []}, {'background': []}],
    [{'list': 'ordered'}, {'list': 'bullet'}, {'list': 'check'}],
    [{'indent': '-1'}, {'indent': '+1'}],
    [{'align': []}],
    ['blockquote', 'code-block'],
    ['link', 'image', 'video'],
    ['table'],
    ['clean'],
]


class QuillWidget(Widget):
    template_name = None

    def __init__(self, label='Description', extract_block=None, *args, **kwargs):
        self.label = label
        self.extract_block = extract_block
        super().__init__(*args, **kwargs)

    def render(self, name, value, attrs=None, renderer=None):
        field_id = (attrs or {}).get('id', f'id_{name}')
        editor_id = f'quill-editor-{field_id}'
        value = value or ''
        toolbar_json = json.dumps(QUILL_TOOLBAR)
        base_name = name.split('-')[-1]
        field_class = f'field-{base_name}'
        extract_block_js = json.dumps(self.extract_block)
        return mark_safe(f'''
<style>
  .{field_class} > div > .flex-container > label {{ display: none !important; }}
  .{field_class} > div > .flex-container {{ display: block !important; }}
</style>
<div class="quill-wrapper">
  <div id="{editor_id}" class="quill-editor-box"></div>
  <textarea name="{name}" id="{field_id}" style="display:none">{escape(value)}</textarea>
</div>
<script>
(function(){{
  var container = document.getElementById('{editor_id}');
  var textarea  = document.getElementById('{field_id}');
  function init() {{
    var quill = new Quill(container, {{
      theme: 'snow',
      modules: {{
        toolbar: {{
          container: {toolbar_json},
          handlers: {{
            'table': function() {{
              var self = this;
              var savedRange = quill.getSelection();
              var existing = document.getElementById('ql-table-picker-{field_id}');
              if (existing) {{ existing.remove(); return; }}
              var btn = quill.container.previousSibling.querySelector('.ql-table');
              var ROWS = 8, COLS = 10;
              var popup = document.createElement('div');
              popup.id = 'ql-table-picker-{field_id}';
              popup.style.cssText = 'position:absolute;background:#fff;border:1px solid #e5e7eb;border-radius:6px;padding:10px;box-shadow:0 4px 12px rgba(0,0,0,.1);z-index:9999;user-select:none;';
              var label = document.createElement('div');
              label.style.cssText = 'font-size:12px;color:#6b7280;margin-bottom:8px;text-align:center;';
              label.textContent = '1 × 1';
              var grid = document.createElement('div');
              grid.style.cssText = 'display:grid;grid-template-columns:repeat('+COLS+',18px);gap:2px;';
              var cells = [];
              for (var r = 1; r <= ROWS; r++) {{
                for (var c = 1; c <= COLS; c++) {{
                  (function(row, col) {{
                    var cell = document.createElement('div');
                    cell.style.cssText = 'width:18px;height:18px;border:1px solid #d1d5db;border-radius:2px;cursor:pointer;box-sizing:border-box;';
                    cell.addEventListener('mouseenter', function() {{
                      label.textContent = row + ' × ' + col;
                      cells.forEach(function(cl) {{
                        var cr = parseInt(cl.dataset.r), cc = parseInt(cl.dataset.c);
                        cl.style.background = (cr <= row && cc <= col) ? '#dbeafe' : '';
                        cl.style.borderColor = (cr <= row && cc <= col) ? '#93c5fd' : '#d1d5db';
                      }});
                    }});
                    cell.addEventListener('click', function() {{
                      popup.remove();
                      var html = '<table style="border-collapse:collapse;width:100%;table-layout:fixed"><tbody>';
                      for (var i = 0; i < row; i++) {{
                        html += '<tr>';
                        for (var j = 0; j < col; j++) {{
                          html += '<td style="border:1px solid #d1d5db;padding:6px 10px;background:#fff;"><br></td>';
                        }}
                        html += '</tr>';
                      }}
                      html += '</tbody></table><p><br></p>';
                      var idx = savedRange ? savedRange.index : quill.getLength();
                      quill.clipboard.dangerouslyPasteHTML(idx, html);
                    }});
                    cell.dataset.r = r; cell.dataset.c = c;
                    cells.push(cell); grid.appendChild(cell);
                  }})(r, c);
                }}
              }}
              popup.appendChild(label); popup.appendChild(grid);
              document.body.appendChild(popup);
              var rect = btn ? btn.getBoundingClientRect() : quill.container.getBoundingClientRect();
              popup.style.left = (rect.left + window.scrollX) + 'px';
              popup.style.top  = (rect.bottom + window.scrollY + 4) + 'px';
              function close(e) {{ if (!popup.contains(e.target) && e.target !== btn) {{ popup.remove(); document.removeEventListener('mousedown', close); }} }}
              setTimeout(function() {{ document.addEventListener('mousedown', close); }}, 0);
            }}
          }}
        }},
      }},
      placeholder: '',
    }});
    container.__quill = quill;  /* for external access (bundle-items-extracted handler) */
    var wrapper = container.closest('.quill-wrapper');
    function processHtml(raw) {{
      var tmp = document.createElement('div');
      tmp.innerHTML = raw;
      tmp.querySelectorAll('th').forEach(function(th) {{
        var td = document.createElement('td');
        td.innerHTML = '<strong>' + th.innerHTML + '</strong>';
        th.parentNode.replaceChild(td, th);
      }});
      ['thead','tbody','tfoot'].forEach(function(tag) {{
        tmp.querySelectorAll(tag).forEach(function(el) {{
          while (el.firstChild) el.parentNode.insertBefore(el.firstChild, el);
          el.remove();
        }});
      }});
      return tmp.innerHTML;
    }}
    var SPINNER_HTML = '<span style="width:14px;height:14px;border:2px solid #d1d5db;border-top-color:#6b7280;border-radius:50%;display:block;animation:ql-btn-spin 0.7s linear infinite;"></span>';
    if (!document.getElementById('ql-btn-spin-style')) {{
      var st = document.createElement('style');
      st.id = 'ql-btn-spin-style';
      st.textContent = '@keyframes ql-btn-spin {{ to {{ transform: rotate(360deg); }} }}';
      document.head.appendChild(st);
    }}
    function setBtnLoading(btnEl, isLoading) {{
      if (isLoading) {{
        if (!btnEl._origHtml) btnEl._origHtml = btnEl.innerHTML;
        btnEl.innerHTML = SPINNER_HTML;
        btnEl.disabled = true;
      }} else {{
        if (btnEl._origHtml) btnEl.innerHTML = btnEl._origHtml;
        btnEl.disabled = false;
      }}
    }}
    function llmRequest(text, promptKey, onSuccess, btnEl) {{
      setBtnLoading(btnEl, true);
      fetch('/admin/catalog/generate-description/', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value}},
        body: JSON.stringify({{text: text, prompt: promptKey}}),
      }}).then(function(r){{ return r.json(); }}).then(function(d){{
        if (d.error) {{ alert('Error: ' + d.error); return; }}
        onSuccess(d);
        if (window.pgMarkDirty) window.pgMarkDirty();
      }}).catch(function(e){{ alert('Error: ' + e); }}).finally(function(){{
        setBtnLoading(btnEl, false);
      }});
    }}
    function makeIconBtn(svgHtml, title) {{
      var b = document.createElement('button');
      b.type = 'button'; b.innerHTML = svgHtml; b.title = title;
      b.style.cssText = 'padding:2px;width:28px;height:28px;display:flex;align-items:center;justify-content:center;color:#6b7280;background:#fff;border:1px solid #d1d5db;border-radius:4px;cursor:pointer;flex-shrink:0;';
      b.addEventListener('mouseenter', function(){{ b.style.borderColor='#9ca3af'; }});
      b.addEventListener('mouseleave', function(){{ b.style.borderColor='#d1d5db'; }});
      return b;
    }}
    var wandSvg = '<img src="data:image/png;base64,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" width="14" height="14" style="display:block;opacity:0.6;">';
    var broomSvg = '<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAYAAAD0eNT6AAAACXBIWXMAAA7DAAAOwwHHb6hkAAAAGXRFWHRTb2Z0d2FyZQB3d3cuaW5rc2NhcGUub3Jnm+48GgAAEvNJREFUeJzt3WnMZQddx/HfMzPtTGlpposFpLVDhUppkdJCumigg0WgViAQVi2UrRgiIgQRfQEECcGAEaK4BI2iJKBoImpc0SBohGgQIrsgSxQtaRWqJZW0HV+cgVI67TzLOfd/z/l/PslNCW/u73nm5pzvc+62EaCDPUkeluRxSS5Ocp8kJyW5Ncl/J/lMkvcleVeSDxz+/wGAmTohySuSXJ/k0CZvX0zyoiTHFuwFAHboGUn+I5s/8X/r7TNJrlj5agBgW3YneV22f+L/5tutSV6VZGOVPwAAsDV7kvxRxjn5f/PtLav8IQCArfmFjH/y//rtZSv8OQCATXp2pjv5H0pyS5JHreynAQCO6u5Jrs20AXAoySeTHLOinwkAOIrXZvqT/9dvz1/RzwQA3IV9SW7I6gLg06v5sQCAu3JlVnfy//rtgSv5yYDJ7KoeAOzY4wru8/EF9wmMSADA/F1acJ+XFNwnMCIBAPN3VsF93rfgPoERCQCYt32Hb6u2v+A+gREJAJi3qm/s21t0v8BIBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABraqB4AzRyb5LIkj09yUZKzkuyvHFTouiSfTfK+JO9K8ndJbildBAAjOzHJzyT5cpJDbke8XZvkpUn2bfN3DABr5TlJvpT6E+xcbp9L8tjt/KIBYB3sSfKm1J9Q53i7Ncnr4nVKAMzMMUn+NPUn0rnffiNeqwTAjPxS6k+eS7n99BZ/9wBQ4nmpP2ku6XZLkiu29C8AHJVLazCu/Uk+neSU6iEL869JzknyteohsBReYAPjenmc/KdwVpLnVo+AJXEFAMZzXIa3+51QPWShPpshBIARuAIA43lknPyndJ8kD6oeAUshAGA8Prxmeo+rHgBLIQBgPJdWD2jgkuoBsBQCAMbj+enpfWf1AFgKAQDj2Jdkb/WIBrp+cyKMTgDAOI6tHtCEyIKRCAAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQkAAAgIYEAAA0JAAAoCEBAAANCQAAaEgAAEBDAgAAGhIAANCQAACAhgQAADQkAACgIQEAAA0JAABoSAAAQEMCAAAaEgAA0JAAAICGBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABoSAADQ0Eb1ANbCiUnOPnw75/B/z0hyfJITkuw//N9jqwYCm3ZTkhuTfCXJDYf/9+eTfCLJJ5N86vDtq1UDWQ8CoKfTkhxM8ojD/71f7RxgxQ4l+WiSvz58+5skXy5dxMoJgD7un+SqJI9Ncm782wO3uSXJPyX5gyS/neQLtXNYBSeBZduf5MlJnpHk0vj3Bo7u1iR/n+S3krw9yf/UzmEqTgjLdL8kL0/yw/G8PbB9NyZ5S5I3JPn34i2MTAAsy3lJXpbkaUn2FG8BluNrSX4nyWsyvICQBRAAy3Bmkp9L8oT4NwWmc3OS30zyU0muq53CTu2uHsCOHJPkhUl+L8n5cfIHprUryQVJnpfh7Yb/mOEdBcyQE8Z8HUzy5gzv2weo8P4kL8jwDgJmxhWA+dmT5BVJfj3D+/kBqpye5DkZziXvjasBs+IKwLyckeFtOd9TPQTgW/xxkquTXF+8g00SAPPxgxlefHNy8Q6AO/OFJE/N8DkCrDlfBjQPL8zwCV1O/sA6+44k78kQAaw5rwFYbxtJXpXkZ+NqDTAPu5M8McMnCLoSsMYEwPraneRXkrykegjAFm0keVSS45K8u3gLd0IArKfdSd6R4ct7AObqe5OclOTPqodwRwJgPb0pybOqRwCM4OIMnyD4vuoh3J4AWD+vSfLS6hEAIzqY5ItJPlg9hNsIgPXygiSvqx4BMLKNJFck+djhG2vAK8vXx8EkfxlRBizXTRmeEvhw9RAEwLq4R4bP0r5X9RCAiX0qyUMyvE2QQj4IqN6uJG+Lkz/Qw9lJfrV6BC43r4NXJnl29QiAFXpgks8n+VD1kM48BVDrwiQfiBAD+vnfDF9n/m/VQ7py4qmzK8nvZ/jsbIBujs3wDafvrB7SldcA1Lkmw6thAbp6Uoa3B1LAUwA1TknyiSSnVg8BKPbpDK8JuKl6SDeeAqjx80keVj0CYA2cnOTGJH9bPaQbVwBW7/QMxbu3egjAmrg+yYEMLwxkRbwGYPVeGid/gG92SpLnVY/oxhWA1To1yeeSHF+8A2Dd/GeS+8RrAVbGFYDVekmc/AGO5J5Jrq4e0YkrAKtzTIavw/TKf4Aj+2iS86pHdOEKwOr8QJz8Ae7KuUnOrx7RhQBYnauqBwDMgGPlingKYDVOznD536v/Ae7al5LcO8nN1UOWzhWA1XhKnPwBNuO0JJdXj+hAAKzGY6oHAMyIY+YKeApgeruTXJdkf/UQgJn4SIbvB2BCrgBM78I4+QNsxbkZPheACQmA6T2iegDAzGwkuax6xNIJgOk9vHoAwAwdrB6wdAJgej7UAmDrHlQ9YOm8CHBaJyb5SvUIgBn6cpKTqkcsmSsA0/qu6gEAM7U/w2cCMBEBMC0BALB9jqETEgDT8uAF2L77Vw9YMgEwLe9jBdg+x9AJCYBp3b16AMCMOYZOSABMy4MXYPscQyckAKZ1QvUAgBkTABMSANPy4AXYPsfQCQmAaR1XPQBgxu5WPWDJBAAANCQAAKAhAQAADQkAAGhIAABAQwIAABraqB6wcOcm2Vc9AmCmbkjyL9UjAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgE3aqB6wYA9P8nPVI47iliQXFdzv7iQfLrjfnXpBkvcW3O+bMzye5uR3k7y64H6flOSVBfe7E9cluazgfk9J8ucF97tVVyX5ePWIJdpTPWDB7pvkwuoRR3Fz0f1uJDm36L534sSi+z0z8/t93bvofk/O/H5X1xbd7zFZ/2NUkpwRATCJXdUDFuzbqgcALIBj6UQEwHQ8aAF2zrF0IgJgOqdVDwBYAAEwEQEwHQ9agJ1zLJ2IAJiOBy3AzjmWTkQATMdTAAA7JwAmIgCmc2r1AIAFEAATEQDT2J9kX/UIgAW4Z/WApRIA0zhQPQBgIU5MclL1iCUSANM4UD0AYEEOVA9YIgEwjQPVAwAW5ED1gCUSANM4s3oAwII4pk5AAEzDgxVgPI6pExAA0zhQPQBgQQ5UD1giATCNA9UDABbkQPWAJRIA4/OWFYBxHagesEQCYHz3rx4AsDD7k9yjesTSCIDxPaB6AMACObaOTACMz4MUYHznVg9YGgEwPg9SgPH542pkAmB8HqQA43NsHZkAGNcJ8YEVAFNwdXVkAmBc5yTZqB4BsECnJjmtesSSCIBxuUQFMB1XAUYkAMZ1QfUAgAV7cPWAJREA43po9QCABXOMHZEAGM+eJOdXjwBYMAEwIgEwnvOSHFc9AmDBzkpySvWIpRAA41GmANPaSHJh9YilEADjeUj1AIAG/LE1EgEwHg9KgOk51o5EAIzj+AyvAQBgWhdVD1gKATCOS5McUz0CoIF7Jjm7esQSCIBxPLx6AEAjl1UPWAIBMA4BALA6jrkjEAA7d1y8KAVglQ5WD1gCAbBzlyTZWz0CoJF7Jblv9Yi5EwA751IUwOo59u6QANi576seANCQY+8OCYCdOSnekwpQ4VFJdlePmDMBsDOPzvAtgACs1snxB9iOCICdeUz1AIDGHIN3QABs364k3189AqCxK6oHzJkA2L6HJLlH9QiAxh6c5NurR8yVANg+5QlQayOeBtg2AbB9T6weAECeUD1grgTA9pwdX/8LsA4emeEt2WyRANieJ1cPACDJ8FXsV1aPmCMBsD0u/wOsD8fkbRAAW3dWkvOrRwDwDY9OcmL1iLkRAFv3lOoBANzO3iSPrR4xNwJg636oegAAd3BV9YC5EQBb89Ak51aPAOAOLk9yevWIOREAW6MwAdbTriRPrx4xJwJg8/bE2/8A1tkzqwfMiQDYvCvjs/8B1tkDMjxVyyYIgM37keoBABzVNdUD5kIAbM5ZGT5uEoD19vT4aOBNEQCbc038rgDm4G7xdu1NcVI7ur1JnlU9AoBNe371gDkQAEf3tCSnVY8AYNPOS3KwesS6EwBH96LqAQBs2YurB6w7AXDXLo8v/gGYoyuTnFM9Yp0JgLv249UDANiWjSQ/Vj1inQmAO3d+kiuqRwCwbVcnuXf1iHUlAO7cKzIUJADztC/JT1SPWFcC4MjOT/L46hEA7Njz4yrAEQmAI/PXP8Ay7EvysuoR60gA3JG//gGW5Zokp1ePWDcC4I789Q+wLK4CHIEAuL2L469/gCW6JsMXu3GYALjNRpI3xl//AEu0N8nrq0esEwFwm6uSXFQ9AoDJPCG+2v0bBMDg+CSvrR4BwOTekGR39Yh1IAAGr473iQJ08N1JXlg9Yh0IgOSC+MY/gE5ek+TM6hHVugfAriRvjstBAJ0cn+SXq0dU6x4AT87w1j8AenlMkodVj6jUPQB8VSRAXy+uHlCpcwAcSHJJ9QgAylyZZH/1iCqdA8B7QQF625Pk8uoRVToHwAXVAwAod2H1gCqdA+CM6gEAlDu7ekCVzgFw9+oBAJTzGoCGbq4eAEC5tl8A1zkAvlQ9AIBy11cPqNI5AD5ZPQCAcp+tHlClcwC8v3oAAOX+oXpAlc4B8J4kN1aPAKDMLUn+qnpElc4BcFOSd1SPAKDMnyT5r+oRVToHQJK8Mcmt1SMAKPGL1QMqdQ+AjyR5a/UIAFbu3Un+onpEpe4BkCQvT3Jt9QgAVuarSX60ekQ1ATB8HsBT44OBADo4lOS58VZwAXDYe5I8K8MrQgFYpkNJfjLJ26uHrAMBcJu3JXlavDUQYIn+L8k1SV5fPWRdCIDbe2eShyb5YPUQAEbzz0kuTvJr1UPWiQC4o49niICrk3ysdgoAO/CxJM9M8uAkHyresnb2VA9YU7dmeHvgW5NckuTRSS5Ncr8kpyQ5oW4aAEdwQ4Yv9vl4ho/3/cO4mnuX/h+E48FNpbYxlgAAAABJRU5ErkJggg==" width="14" height="14" style="display:block;opacity:0.6;">';
    var extractBlock = {extract_block_js};
    var extractBtn = null;
    if (extractBlock) {{
      var wandTitle = (extractBlock === 'bundle_items') ? 'Fill the bundle composition' : 'Extract data from the text';
      extractBtn = makeIconBtn(wandSvg, wandTitle);
      extractBtn.addEventListener('click', function(){{
        if (!quill.getText().trim()) {{ alert('The editor is empty'); return; }}
        if (extractBlock === 'bundle_items') {{
          /* Bundle: we extract the full package — name/general_name/variant_name +
             items + categories + attributes. We dispatch an event. */
          llmRequest(quill.root.innerHTML, 'bundle_items', function(d) {{
            /* description + application_norms are filled in by the bundle-items-extracted
               handler into the active packaging — we change nothing here. */
            document.dispatchEvent(new CustomEvent('bundle-items-extracted', {{
              detail: {{
                general_name: d.general_name || '',
                name: d.name || '',
                variant_name: d.variant_name || '',
                items: d.items || [],
                match_error: d.match_error || '',
                categories: d.categories || [],
                existing_bundle: d.existing_bundle || null,
                description: d.description || '',
                application_norms: d.application_norms || '',
              }}
            }}));
          }}, extractBtn);
          return;
        }}
        if (extractBlock === 'product_fill') {{
          /* Exactly like the button on the "Raw text" tab: the same request
             (prompt 'extract') and the same "Fill in the fields" window. A direct fetch
             (not llmRequest), so as not to mark the form dirty before the actual filling. */
          setBtnLoading(extractBtn, true);
          var restore = function() {{ setBtnLoading(extractBtn, false); }};
          fetch('/admin/catalog/generate-description/', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value}},
            body: JSON.stringify({{text: quill.root.innerHTML, prompt: 'extract'}}),
          }}).then(function(r) {{ return r.json(); }}).then(function(d) {{
            if (d.error) {{ alert('Error: ' + d.error); restore(); return; }}
            if (window._pgShowFillModal) window._pgShowFillModal(d);
            else alert('The fill-in window is not available');
            restore();
          }}).catch(function(e) {{ alert('Error: ' + e); restore(); }});
          return;
        }}
        llmRequest(quill.root.innerHTML, 'extract', function(d) {{
          var raw = '';
          if (extractBlock === 'description') raw = d.description_html || '';
          else if (extractBlock === 'application_norms') raw = d.application_norms_html || '';
          if (!raw) return;
          var html = processHtml(raw);
          if (!html) return;
          quill.setContents([]);
          quill.clipboard.dangerouslyPasteHTML(html);
        }}, extractBtn);
      }});
    }}
    var cleanBtn = makeIconBtn(broomSvg, 'Improve the selected text');
    cleanBtn.addEventListener('click', function(){{
      var range = quill.getSelection();
      if (!range || range.length === 0) {{ alert('Select some text in the editor'); return; }}
      var text = quill.getText(range.index, range.length);
      if (!text.trim()) {{ alert('Select some text'); return; }}
      var savedRange = range;
      llmRequest(text, 'clean', function(d) {{
        var html = d.html ? processHtml(d.html) : '';
        if (!html) {{ alert('Nothing found'); return; }}
        quill.deleteText(savedRange.index, savedRange.length);
        quill.clipboard.dangerouslyPasteHTML(savedRange.index, html);
      }}, cleanBtn);
    }});
    var arrowColor = '#6b7280';
    var arrowColorDark = '#111827';
    function arrowSpan(up) {{ return '<span style="display:block;width:7px;height:7px;border-right:2px solid '+arrowColor+';border-bottom:2px solid '+arrowColor+';transform:rotate('+(up?'-135deg':'45deg')+');margin-top:'+(up?'3px':'-3px')+';"></span>'; }}
    function arrowSpanDark(up) {{ return '<span style="display:block;width:7px;height:7px;border-right:2px solid '+arrowColorDark+';border-bottom:2px solid '+arrowColorDark+';transform:rotate('+(up?'-135deg':'45deg')+');margin-top:'+(up?'3px':'-3px')+';"></span>'; }}
    var lbl = document.createElement('div');
    lbl.style.cssText = 'font-size:11px;font-weight:700;color:#6b7280;letter-spacing:0.5px;text-transform:uppercase;padding:8px 12px;background:#f9fafb;display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none;';
    var isOpen = false;
    lbl.addEventListener('mouseenter', function(){{ lblText.style.color='#111827'; rawArrowBtn.innerHTML=arrowSpanDark(isOpen); }});
    lbl.addEventListener('mouseleave', function(){{ lblText.style.color=''; rawArrowBtn.innerHTML=arrowSpan(isOpen); }});
    var lblText = document.createElement('span');
    lblText.textContent = '{self.label}';
    lbl.appendChild(lblText);
    var rawArrowBtn = document.createElement('button');
    rawArrowBtn.type = 'button';
    rawArrowBtn.innerHTML = arrowSpan(false);
    rawArrowBtn.style.cssText = 'padding:0;width:16px;height:16px;flex-shrink:0;display:flex;align-items:center;justify-content:center;background:transparent;border:none;cursor:pointer;';
    lbl.appendChild(rawArrowBtn);
    var qlToolbar = container.previousElementSibling;
    if (qlToolbar) {{
      var lastGroup = qlToolbar.querySelector('.ql-formats:last-child');
      if (lastGroup) {{
        lastGroup.style.display = 'inline-flex'; lastGroup.style.gap = '4px'; lastGroup.style.alignItems = 'center';
        lastGroup.appendChild(cleanBtn);
        if (extractBtn) lastGroup.appendChild(extractBtn);
      }} else {{
        qlToolbar.appendChild(cleanBtn);
        if (extractBtn) qlToolbar.appendChild(extractBtn);
      }}
    }}
    function showEditor() {{
      container.style.display = ''; if (qlToolbar) qlToolbar.style.display = '';
      wrapper.style.minHeight = ''; wrapper.style.resize = ''; wrapper.style.marginBottom = '';
      isOpen = true;
      lbl.style.borderBottom = '1px solid #e5e7eb';
      rawArrowBtn.innerHTML = arrowSpan(true);
    }}
    function hideEditor() {{
      container.style.display = 'none'; if (qlToolbar) qlToolbar.style.display = 'none';
      wrapper.style.minHeight = '0'; wrapper.style.height = ''; wrapper.style.resize = 'none'; wrapper.style.marginBottom = '0';
      isOpen = false;
      lbl.style.borderBottom = 'none';
      rawArrowBtn.innerHTML = arrowSpan(false);
    }}
    function toggleEditor() {{ if (container.style.display !== 'none') hideEditor(); else showEditor(); }}
    hideEditor();
    rawArrowBtn.addEventListener('click', function(e){{ e.stopPropagation(); toggleEditor(); }});
    lbl.style.cursor = 'pointer';
    lbl.addEventListener('click', function(e) {{
      if (rawArrowBtn.contains(e.target)) return;
      toggleEditor();
    }});
    wrapper.insertBefore(lbl, wrapper.firstChild);
    function trimEmptyParas(html) {{
      var tmp = document.createElement('div');
      tmp.innerHTML = html;
      var isEmpty = function(el) {{ return el.tagName === 'P' && (el.innerHTML === '<br>' || el.innerHTML === ''); }};
      while (tmp.firstElementChild && isEmpty(tmp.firstElementChild)) tmp.firstElementChild.remove();
      while (tmp.lastElementChild && isEmpty(tmp.lastElementChild)) tmp.lastElementChild.remove();
      return tmp.innerHTML;
    }}
    function getCleanHtml() {{
      var h = quill.root.innerHTML;
      return (h === '<p><br></p>' || !h) ? '' : trimEmptyParas(h);
    }}
    window._quillInstances = window._quillInstances || {{}};
    window._quillInstances['{name}'] = quill;
    /* We let external code expand/collapse the block (AI fill-in, Cancel). */
    wrapper._openEditor = showEditor;
    wrapper._closeEditor = hideEditor;
    var raw = textarea.value;
    if (raw) {{ quill.clipboard.dangerouslyPasteHTML(trimEmptyParas(raw)); showEditor(); }}
    quill.on('text-change', function(delta, old, source) {{
      textarea.value = getCleanHtml();
      if (source === 'user' && window.pgMarkDirty) window.pgMarkDirty();
    }});
    var form = textarea.closest('form');
    if (form) form.addEventListener('submit', function() {{
      textarea.value = getCleanHtml();
    }});
  }}
  if (typeof Quill !== 'undefined') {{ init(); }}
  else {{ document.addEventListener('DOMContentLoaded', init); }}
}})();
</script>
''')

    def value_from_datadict(self, data, files, name):
        return data.get(name, '')
