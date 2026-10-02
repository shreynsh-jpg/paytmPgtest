(function () {
  "use strict";

  var cfg = window.CHECKOUT_CONFIG;
  var form = document.getElementById("pay-form");
  var btn = document.getElementById("pay-btn");
  var label = document.getElementById("pay-label");
  var errorBox = document.getElementById("error");
  var amountInput = document.getElementById("amount");
  var summaryAmount = document.getElementById("summary-amount");
  var paytmScript = null;

  function formatINR(value) {
    var n = parseFloat(value);
    if (!isFinite(n) || n < 0) n = 0;
    return "₹" + n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function updateSummary() {
    summaryAmount.textContent = formatINR(amountInput.value);
    label.textContent = amountInput.value ? "Pay " + formatINR(amountInput.value) : "Pay securely";
  }

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.classList.add("show");
  }

  function setLoading(on) {
    btn.disabled = on;
    btn.classList.toggle("loading", on);
  }

  // Load Paytm's merchant-specific JS Checkout script once, on demand.
  function loadPaytm() {
    if (paytmScript) return paytmScript;
    paytmScript = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = cfg.checkoutJsUrl;
      s.crossOrigin = "anonymous";
      s.onload = function () {
        if (window.Paytm && window.Paytm.CheckoutJS) {
          window.Paytm.CheckoutJS.onLoad(resolve);
        } else {
          reject(new Error("Paytm checkout failed to initialise."));
        }
      };
      s.onerror = function () {
        paytmScript = null;
        reject(new Error("Could not load Paytm checkout. Check your connection."));
      };
      document.head.appendChild(s);
    });
    return paytmScript;
  }

  function openPaytm(order) {
    var config = {
      root: "",
      flow: "DEFAULT",
      data: {
        orderId: order.orderId,
        token: order.txnToken,
        tokenType: "TXN_TOKEN",
        amount: order.amount
      },
      merchant: { redirect: true }, // Paytm posts the result to our /payment/callback
      handler: {
        notifyMerchant: function (eventName) {
          if (eventName === "APP_CLOSED") {
            setLoading(false);
            showError("Payment window closed. You can try again.");
          }
        }
      }
    };
    return window.Paytm.CheckoutJS.init(config).then(function () {
      window.Paytm.CheckoutJS.invoke();
    });
  }

  document.querySelectorAll(".quick button").forEach(function (b) {
    b.addEventListener("click", function () {
      amountInput.value = b.dataset.amount;
      updateSummary();
    });
  });
  amountInput.addEventListener("input", updateSummary);

  document.getElementById("phone").addEventListener("input", function (e) {
    e.target.value = e.target.value.replace(/\D/g, "").slice(0, 10);
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    errorBox.classList.remove("show");

    if (!form.reportValidity()) return;

    setLoading(true);
    // Start loading Paytm's script in parallel with creating the order.
    var paytmReady = cfg.mockMode ? Promise.resolve() : loadPaytm();

    fetch("/api/initiate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        amount: amountInput.value,
        name: document.getElementById("name").value,
        email: document.getElementById("email").value,
        phone: document.getElementById("phone").value
      })
    })
      .then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok) throw new Error(data.error || "Something went wrong.");
          return data;
        });
      })
      .then(function (order) {
        if (order.mock) {
          window.location.href = order.redirect;
          return;
        }
        return paytmReady.then(function () { return openPaytm(order); });
      })
      .catch(function (err) {
        setLoading(false);
        showError(err.message);
      });
  });

  updateSummary();
})();
